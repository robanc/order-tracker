import importlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "incident-response"))
responder = importlib.import_module("server")
context = importlib.import_module("context")
agent = importlib.import_module("agent")

TEST = {"alerts": [{"status": "firing", "labels": {"alertname": "ResponderTest", "test": "true"},
                    "annotations": {"summary": "Test notification; no incident to fix"}}]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "STORE", tmp_path / "incidents")
    with TestClient(responder.app) as client:
        yield client


def test_test_alert_one_invocation_and_durable_dedup(client, monkeypatch):
    run = Mock(return_value={"status": "completed", "response": "Synthetic test.\nNo fix needed.",
                             "last_line": "No fix needed.", "attempts": 1})
    monkeypatch.setattr(responder, "run_agent", run)
    fetch = Mock(side_effect=AssertionError("Test notification must not query telemetry"))
    monkeypatch.setattr(context, "fetch", fetch)
    response = client.post("/alerts", json=TEST)
    assert response.status_code == 202
    result = client.get(response.json()["result_url"]).json()
    assert result["last_line"] == "No fix needed."
    assert client.post("/alerts", json=TEST).json()["status"] == "duplicate"
    run.assert_called_once()
    assert run.call_args.args[0]["synthetic_test"] is True
    fetch.assert_not_called()
    saved = next(responder.STORE.glob("*/context.json")).read_text()
    assert "Test notification; no incident to fix" in saved


@pytest.mark.parametrize("payload", [{}, {"alerts": []}, {"alerts": [None]},
                                      {"alerts": [{"status": "firing", "labels": []}]}])
def test_invalid_alerts(client, payload):
    assert client.post("/alerts", json=payload).status_code == 400


def test_resolved_and_input_limits(client, monkeypatch):
    run = Mock()
    monkeypatch.setattr(responder, "run_agent", run)
    assert client.post("/alerts", json={"alerts": [{"status": "resolved"}]}).json()["status"] == "ignored"
    assert client.post("/alerts", content=b"x" * 65537, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/alerts", json=TEST, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/alerts", content="bad").status_code == 415
    assert client.get("/incidents/not-an-id").status_code == 404
    run.assert_not_called()


def test_sanitized_metadata_and_telemetry(monkeypatch):
    secret = "sensitive-canary"
    parsed = context.parse_alerts({"alerts": [{"status": "firing", "labels": {
        "alertname": "Order lookup HTTP 5xx", "customer": secret, "authorization": secret},
        "annotations": {"route": "GET " + context.ROUTE, "summary": secret, "body": secret},
        "generatorURL": "https://" + secret, "request": secret}]})
    trace_id = "a" * 32
    def fetch(url, params=None):
        if ":9090" in url:
            return {"data": {"result": [{"metric": {"http_response_status_code": "500", "token": secret}, "value": [1, "2"]}]}}
        if ":3100" in url:
            return {"data": {"result": [{"stream": {"http_route": context.ROUTE, "order_id": secret,
                    "http_response_status_code": "500", "trace_id": trace_id}, "values": [["12345", secret]]}]}}
        return {"batches": [{"scopeSpans": [{"spans": [{"name": secret, "attributes": [
            {"key": "http.route", "value": {"stringValue": context.ROUTE}},
            {"key": "http.response.status_code", "value": {"intValue": "500"}},
            {"key": "authorization", "value": {"stringValue": secret}}],
            "events": [{"name": "exception", "attributes": [
                {"key": "exception.type", "value": {"stringValue": "builtins.ValueError"}},
                {"key": "exception.message", "value": {"stringValue": "day is out of range for month"}},
                {"key": "exception.stacktrace", "value": {"stringValue": secret}},
                {"key": "authorization", "value": {"stringValue": secret}},
            ]}], "status": {"message": secret}}]}]}]}
    monkeypatch.setattr(context, "fetch", fetch)
    data = context.collect_context(parsed)
    assert secret not in json.dumps(data)
    assert data["metrics"] == [{"status_code": 500, "increase_5m": 2}]
    assert data["logs"][0]["trace_id"] == trace_id
    assert data["traces"][0]["spans"][0]["http_response_status_code"] == 500
    assert data["traces"][0]["spans"][0]["exceptions"] == [
        {"type": "ValueError", "message": "day is out of range for month"}]
    assert data["source"]["file"] == "app/main.py"
    assert data["source"]["imports"]
    assert {item["name"] for item in data["source"]["functions"]} == {
        "as_dict", "order_detail", "get_order"}


def test_exception_message_allowlist_and_escaped_flag():
    assert context.safe_exception({
        "exception.type": "builtins.ValueError",
        "exception.message": "customer private order contents",
        "exception.escaped": "True",
        "exception.stacktrace": "secret-token",
    }) == {"type": "ValueError", "escaped": True}


def test_source_context_reads_only_selected_functions(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    app_dir = source_root / "app"
    app_dir.mkdir(parents=True)
    source = app_dir / "main.py"
    source.write_text(
        "def order_detail(row):\n    return row['status']\n\n"
        "def get_order(order_id):\n    return order_detail(order_id)\n\n"
        "def as_dict(row):\n    return row\n\n"
        "def seed_customer_data():\n    return 'private-customer-secret'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(context, "APP_ROOT", source_root)
    monkeypatch.setattr(context, "MAIN_SOURCE", source)
    selected = context.source_context()
    serialized = json.dumps(selected)
    assert {item["name"] for item in selected["functions"]} == {
        "as_dict", "order_detail", "get_order"}
    assert selected["imports"] == []
    assert "seed_customer_data" not in serialized
    assert "private-customer-secret" not in serialized


def test_unavailable_telemetry_does_not_leak_errors(monkeypatch):
    monkeypatch.setattr(context, "fetch", Mock(side_effect=RuntimeError("secret-token")))
    result = context.collect_context(context.parse_alerts({"alerts": [{"status": "firing"}]}))
    assert result["metrics"] == {"unavailable": True}
    assert "secret-token" not in json.dumps(result)


def test_agent_permissions_and_complete_response(monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setenv("SECRET_KEY", "do-not-inherit")
    answer = "This is a test.\nNo incident to fix."
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"result": answer}), "private diagnostic"))
    monkeypatch.setattr(agent.subprocess, "run", run)
    result = agent.run_agent({"synthetic_test": True})
    assert result["response"] == answer
    assert result["last_line"] == "No incident to fix."
    command = run.call_args.args[0]
    assert command[command.index("--tools") + 1] == ""
    assert "--safe-mode" in command and "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "SECRET_KEY" not in run.call_args.kwargs["env"]
    assert run.call_args.kwargs["shell"] is False
    assert "order-tracker" not in run.call_args.kwargs["cwd"]
    run.assert_called_once()


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("claude", 1), RuntimeError("credential")])
def test_agent_never_retries(monkeypatch, failure):
    monkeypatch.setattr(agent.shutil, "which", lambda _: "claude.exe")
    run = Mock(side_effect=failure)
    monkeypatch.setattr(agent.subprocess, "run", run)
    result = agent.run_agent({"synthetic_test": True})
    assert result["status"] in {"failed", "timed_out"}
    assert "credential" not in json.dumps(result)
    run.assert_called_once()


def test_failed_incident_does_not_relaunch(client, monkeypatch):
    run = Mock(return_value={"status": "timed_out", "attempts": 1})
    monkeypatch.setattr(responder, "run_agent", run)
    response = client.post("/alerts", json=TEST)
    assert client.get(response.json()["result_url"]).json()["status"] == "timed_out"
    assert client.post("/alerts", json=TEST).json()["status"] == "duplicate"
    run.assert_called_once()

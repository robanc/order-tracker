"""Strict allowlists: raw webhook fields, log bodies and exception text never persist."""
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

ROUTE = "/api/orders/{order_id}"
TEST_SUMMARY = "Test notification; no incident to fix"
SAFE_VALUES = {
    "alertname": {"ResponderTest", "Order lookup HTTP 5xx"},
    "test": {"true", "false"},
    "service": {"order-tracker"},
    "severity": {"warning", "critical", "info"},
    "grafana_folder": {"Order Tracker"},
    "route": {ROUTE, "GET " + ROUTE},
    "http_route": {ROUTE},
    "summary": {TEST_SUMMARY, "HTTP 5xx detected for GET " + ROUTE},
    "description": {"A 5xx counter increased or first appeared for the order lookup endpoint within the last 5 minutes. Evaluated every 10 seconds; no pending period."},
    "evaluation_window": {"5 minutes"},
    "dashboard_url": {"http://localhost:3001/d/order-lookups"},
    "__dashboardUid__": {"order-lookups"},
    "__panelId__": {"3"},
}


def safe_fields(fields):
    return {key: value for key, value in fields.items()
            if key in SAFE_VALUES and isinstance(value, str) and value in SAFE_VALUES[key]}


def parse_alerts(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        raise ValueError("Expected an alerts array")
    if not 1 <= len(payload["alerts"]) <= 20:
        raise ValueError("Expected 1 to 20 alerts")
    output = []
    for alert in payload["alerts"]:
        if not isinstance(alert, dict) or alert.get("status") not in {"firing", "resolved"}:
            raise ValueError("Invalid alert status")
        if any(not isinstance(alert.get(key, {}), dict) for key in ("labels", "annotations")):
            raise ValueError("Invalid alert metadata")
        if alert["status"] == "resolved":
            continue
        clean = {"status": "firing", "labels": safe_fields(alert.get("labels", {})),
                 "annotations": safe_fields(alert.get("annotations", {}))}
        started = alert.get("startsAt")
        if isinstance(started, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z", started):
            try:
                clean["startsAt"] = datetime.fromisoformat(started.replace("Z", "+00:00")).isoformat()
            except ValueError:
                pass
        clean["synthetic_test"] = clean["labels"].get("test") == "true"
        output.append(clean)
    return output


def fetch(url, params=None):
    if params:
        url += "?" + urlencode(params)
    # Local telemetry only; ignore user proxy configuration and payload URLs.
    with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("Telemetry response too large")
    return json.loads(data)


def safe_metadata(fields):
    result = {}
    for key, value in fields.items():
        if key == "http_route" and value == ROUTE:
            result[key] = value
        elif key == "http_request_method" and value == "GET":
            result[key] = value
        elif key == "http_response_status_code" and re.fullmatch(r"[1-5]\d{2}", str(value)):
            result[key] = int(value)
        elif key in {"trace_id", "span_id"} and isinstance(value, str):
            if re.fullmatch(r"[0-9a-f]{32}" if key == "trace_id" else r"[0-9a-f]{16}", value):
                result[key] = value
        elif key == "severity_text" and value in {"INFO", "WARN", "ERROR"}:
            result[key] = value
    return result


def collect_context(alerts):
    context = {"alerts": alerts, "window_seconds": 300, "captured_at_unix": int(time.time())}
    if all(alert["synthetic_test"] for alert in alerts):
        context["synthetic_test"] = True
        context["telemetry"] = "Not queried: test notification, no incident to investigate."
        return context
    context["route"] = ROUTE
    now = int(time.time())
    selector = 'order_lookup_requests_total{service_name="order-tracker",http_route="/api/orders/{order_id}"}'
    try:
        data = fetch("http://127.0.0.1:9090/api/v1/query", {
            "query": "sum by (http_response_status_code) (increase(" + selector + "[5m]))"})
        points = []
        for row in data["data"]["result"][:20]:
            code = safe_metadata(row.get("metric", {})).get("http_response_status_code")
            value = float(row["value"][1])
            if code and math.isfinite(value):
                points.append({"status_code": code, "increase_5m": value})
        context["metrics"] = points
    except Exception:
        context["metrics"] = {"unavailable": True}
    logs = []
    try:
        data = fetch("http://127.0.0.1:3100/loki/api/v1/query_range", {
            "query": '{service_name="order-tracker"} | http_route="/api/orders/{order_id}" | http_response_status_code=~"5.."',
            "start": str((now - 300) * 1_000_000_000), "end": str(now * 1_000_000_000), "limit": 20})
        for stream in data["data"]["result"][:20]:
            for entry in stream.get("values", [])[:20]:
                metadata = {**stream.get("stream", {}), **(entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {})}
                record = safe_metadata(metadata)
                if str(entry[0]).isdigit():
                    record["timestamp_ns"] = str(entry[0])[:20]
                logs.append(record)  # Intentionally discard entry[1], the log body.
        context["logs"] = logs[:20]
    except Exception:
        context["logs"] = {"unavailable": True}
    traces = []
    for trace_id in sorted({log["trace_id"] for log in logs if "trace_id" in log})[:3]:
        trace = {"trace_id": trace_id}
        try:
            data = fetch("http://127.0.0.1:3200/api/traces/" + trace_id)
            spans = []
            for batch in data.get("batches", data.get("resourceSpans", []))[:10]:
                for scope in batch.get("scopeSpans", [])[:10]:
                    for span in scope.get("spans", [])[:20]:
                        attrs = {a["key"].replace(".", "_"): next(iter(a.get("value", {}).values()), None)
                                 for a in span.get("attributes", []) if "key" in a}
                        if attrs.get("http_route") == ROUTE:
                            spans.append(safe_metadata(attrs))
            trace["spans"] = spans[:20]  # No events, stack traces, resources or free text.
        except Exception:
            trace["unavailable"] = True
        traces.append(trace)
    context["traces"] = traces
    return context

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def test_express_order_estimated_delivery_crosses_month_end(client):
    with main.connect() as db:
        db.execute(
            "UPDATE orders SET created_at = ? WHERE id = ?",
            ("2026-01-31T23:30:00+00:00", "express-1002"),
        )

    response = client.get("/api/orders/express-1002")

    assert response.status_code == 200
    assert response.json()["estimated_delivery"] == "2026-02-02"


@pytest.mark.parametrize("order_id,status_code", [("standard-1001", 200), ("missing", 404)])
def test_lookup_telemetry(client, monkeypatch, order_id, status_code):
    from unittest.mock import Mock

    telemetry = main.app.state.lookup_telemetry
    requests = Mock()
    logger = Mock()
    monkeypatch.setattr(telemetry, "requests", requests)
    monkeypatch.setattr(telemetry, "logger", logger)
    spans = []
    monkeypatch.setattr(telemetry.traces._active_span_processor, "on_end", spans.append)

    response = client.get(f"/api/orders/{order_id}")
    assert response.status_code == status_code
    attributes = {
        "http.route": "/api/orders/{order_id}",
        "http.request.method": "GET",
        "http.response.status_code": status_code,
    }
    requests.add.assert_called_once_with(1, attributes)
    assert logger.emit.call_args.kwargs["attributes"] == {**attributes, "order.id": order_id}
    assert len(spans) == 1
    assert dict(spans[0].attributes) == attributes


def test_lookup_failure_telemetry(client, monkeypatch):
    from unittest.mock import Mock

    telemetry = main.app.state.lookup_telemetry
    requests = Mock()
    monkeypatch.setattr(telemetry, "requests", requests)

    def fail(_row):
        raise ValueError("lookup failure")

    monkeypatch.setattr(main, "order_detail", fail)
    with pytest.raises(ValueError, match="lookup failure"):
        client.get("/api/orders/standard-1001")
    assert requests.add.call_args.args[1]["http.response.status_code"] == 500


def test_other_routes_do_not_count_as_lookups(client, monkeypatch):
    from unittest.mock import Mock

    requests = Mock()
    monkeypatch.setattr(main.app.state.lookup_telemetry, "requests", requests)
    client.get("/healthz")
    client.get("/api/orders")
    response = client.post("/api/orders", json={"customer": "Taylor", "item": "Mug"})
    client.patch(f"/api/orders/{response.json()['id']}", json={"status": "shipped"})
    requests.add.assert_not_called()

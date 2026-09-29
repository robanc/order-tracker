from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from app.telemetry import LookupTelemetry


def test_otlp_exports_all_signals(monkeypatch):
    received = {}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    telemetry = LookupTelemetry()
    attributes = {"http.route": "/api/orders/{order_id}", "http.response.status_code": 404}
    try:
        with telemetry.tracer.start_as_current_span("order lookup", attributes=attributes):
            telemetry.requests.add(1, attributes)
            telemetry.logger.emit(body="Order lookup completed", attributes=attributes)
        assert telemetry.traces.force_flush()
        assert telemetry.logs.force_flush()
        assert telemetry.metrics.force_flush()
    finally:
        telemetry.shutdown()
        server.shutdown()
        server.server_close()
        thread.join()

    metrics = ExportMetricsServiceRequest.FromString(received["/v1/metrics"])
    metric = metrics.resource_metrics[0].scope_metrics[0].metrics[0]
    assert metric.name == "order_lookup.requests"
    point = metric.sum.data_points[0]
    assert point.as_int == 1
    assert {a.key: a.value for a in point.attributes}["http.response.status_code"].int_value == 404
    logs = ExportLogsServiceRequest.FromString(received["/v1/logs"])
    record = logs.resource_logs[0].scope_logs[0].log_records[0]
    assert record.body.string_value == "Order lookup completed"
    traces = ExportTraceServiceRequest.FromString(received["/v1/traces"])
    span = traces.resource_spans[0].scope_spans[0].spans[0]
    assert span.name == "order lookup"
    assert record.trace_id == span.trace_id
    assert record.span_id == span.span_id

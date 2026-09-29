import os

from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from opentelemetry._logs import SeverityNumber
from opentelemetry.propagate import extract
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode


class LookupTelemetry:
    def __init__(self):
        use_otlp = bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
        resource = Resource.create({"service.name": "order-tracker"})
        self.traces = TracerProvider(resource=resource)
        self.traces.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter()) if use_otlp
            else SimpleSpanProcessor(ConsoleSpanExporter())
        )
        self.tracer = self.traces.get_tracer(__name__)
        self.logs = LoggerProvider(resource=resource)
        self.logs.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter()) if use_otlp
            else SimpleLogRecordProcessor(ConsoleLogRecordExporter())
        )
        self.logger = self.logs.get_logger(__name__)
        self.metrics = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(
                OTLPMetricExporter() if use_otlp else ConsoleMetricExporter(),
                export_interval_millis=1000,
            )],
        )
        self.requests = self.metrics.get_meter(__name__).create_counter(
            "order_lookup.requests", unit="{request}", description="Order lookup HTTP requests",
        )

    def shutdown(self):
        self.metrics.shutdown()
        self.logs.shutdown()
        self.traces.shutdown()


class OrderLookupRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def instrumented(request):
            telemetry = request.app.state.lookup_telemetry
            attributes = {"http.route": self.path, "http.request.method": request.method}
            with telemetry.tracer.start_as_current_span(
                f"{request.method} {self.path}", kind=SpanKind.SERVER,
                context=extract(request.headers), attributes=attributes,
            ) as span:
                status_code = 500
                try:
                    response = await handler(request)
                    status_code = response.status_code
                    return response
                except HTTPException as exc:
                    status_code = exc.status_code
                    raise
                finally:
                    attributes["http.response.status_code"] = status_code
                    span.set_attribute("http.response.status_code", status_code)
                    if status_code >= 500:
                        span.set_status(StatusCode.ERROR)
                    telemetry.requests.add(1, attributes)
                    telemetry.logger.emit(
                        body="Order lookup completed",
                        severity_number=SeverityNumber.ERROR if status_code >= 500 else SeverityNumber.INFO,
                        severity_text="ERROR" if status_code >= 500 else "INFO",
                        attributes={**attributes, "order.id": request.path_params["order_id"]},
                    )

        return instrumented

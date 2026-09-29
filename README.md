# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Local telemetry (Question 3)

`docker compose up --build -d --wait` starts the app, OpenTelemetry Collector,
Prometheus, Loki, Tempo, and Grafana. Compose sets `OTEL_EXPORTER_OTLP_ENDPOINT`
so the app sends all three signals over OTLP/HTTP to the Collector. Without that
environment variable, the app retains the Question 2 console exporters.

The Collector exposes metrics for Prometheus to scrape every five seconds,
forwards logs to Loki's native OTLP endpoint, and forwards traces to Tempo.
Logs and traces are batched; allow about 15 seconds after a lookup for querying.
Configuration and the provisioned dashboard live under `observability/`.

- Grafana: <http://localhost:3001/d/order-lookups> (override with `GRAFANA_PORT`;
  anonymous Viewer access;
  local admin login is `admin` / `admin`). The Order Tracker dashboard shows
  cumulative request counts by HTTP status, combined 4xx/5xx errors, and logs.
  Counters reset when the application restarts; these are not time-window totals.
- Prometheus: <http://localhost:9090>, query
  `order_lookup_requests_total{service_name="order-tracker",http_route="/api/orders/{order_id}"}`.
- Loki: <http://localhost:3100/ready>. In Grafana Explore, select Loki and query
  `{service_name="order-tracker"} | order_id="standard-1002"`.
  Expand a record to inspect `http_response_status_code`, `trace_id`, and `span_id`.
- Tempo: <http://localhost:3200/ready>. In Grafana Explore, select Tempo and
  search by the trace ID from the log.
- Collector health: <http://localhost:13133/>. The Collector's minimal image
  has no HTTP client for a Compose healthcheck; verify this endpoint directly.

Run the Question 3 lookup exactly as specified:

```powershell
curl.exe -i http://localhost:8000/api/orders/standard-1002
```

Read its actual status from the response and telemetry. All published ports
bind to loopback. Telemetry uses local Docker volumes; no external service is
required.

## HTTP 5xx alert (Question 4)

Grafana provisions `Order lookup HTTP 5xx` from
`observability/grafana/provisioning/alerting/order-lookup-5xx.json`.
It checks GET `/api/orders/{order_id}` every 10 seconds over a 5-minute window
and fires when query A is greater than zero, with no pending period.
The query detects counter increases and newly appearing 5xx series (the app
does not export a status series until that status first occurs). It returns
zero when no matching errors exist. No Data maps to Normal; query execution
errors remain Error. The query is a detection signal, not an exact error count.

The rule annotations include the route, window, and
<http://localhost:3001/d/order-lookups>, and link to dashboard panel 3.
If you override `GRAFANA_PORT`, update the annotation's absolute URL as well.
See the rule at <http://localhost:3001/alerting/grafana/order-lookup-5xx/view>.
No webhook, notification integration, or incident responder is added.

Provisioning loads at Grafana startup. After editing the rule in an existing
stack, run `docker compose restart grafana`; fresh stacks load it automatically.
Validate the query against synthetic metrics with
`uv run --frozen python tests/check_alert_query.py` while Prometheus is running.
After a lookup, allow 30 seconds for export, scraping, and evaluation, then
inspect the actual rule state in Grafana.

"""Test the provisioned PromQL with promtool, without injecting live telemetry."""
import json
from pathlib import Path
import subprocess


root = Path(__file__).resolve().parents[1]
config = json.loads((root / "observability/grafana/provisioning/alerting/order-lookup-5xx.json").read_text())
expression = config["groups"][0]["rules"][0]["data"][0]["model"]["expr"]


def series(status="500", route="/api/orders/{order_id}", values="1+0x10"):
    return {
        "series": 'order_lookup_requests_total{service_name="order-tracker",'
        f'http_route="{route}",http_request_method="GET",http_response_status_code="{status}"}}',
        "values": values,
    }


def case(name, inputs, checks):
    return {
        "name": name,
        "interval": "1m",
        "input_series": inputs,
        "promql_expr_test": [
            {"expr": f"({expression}) > bool 0", "eval_time": at,
             "exp_samples": [{"labels": "{}", "value": value}]}
            for at, value in checks
        ],
    }


tests = [
    case("no traffic returns zero", [], [("1m", 0)]),
    case("404 is not a server error", [series("404")], [("1m", 0)]),
    case("other routes excluded", [series(route="/healthz")], [("1m", 0)]),
    case("first 500 detected then expires", [series()], [("0m", 1), ("2m", 1), ("6m", 0)]),
    case("existing 503 counter increases then recovers",
         [series("503", values="1+0x5 2+0x6")], [("6m", 1), ("12m", 0)]),
    case("counter reset with new error", [series(values="3+0x5 1+0x6")], [("6m", 1), ("12m", 0)]),
]
result = subprocess.run(
    ["docker", "compose", "exec", "-T", "prometheus", "promtool", "test", "rules", "/dev/stdin"],
    input=json.dumps({"evaluation_interval": "1m", "tests": tests}),
    text=True, cwd=root,
)
raise SystemExit(result.returncode)

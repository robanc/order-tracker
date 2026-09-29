# Question 5: local incident responder

This service uses the already installed/authenticated **Claude Code** CLI in
non-interactive mode. It runs natively on Windows because the available CLI and
its login are host-local. Compose continues to run the telemetry services.
No credentials, host auth directory, repository, or Docker socket are mounted
into a responder container. No additional dependencies are required.

From the repository root:

```powershell
uv sync --frozen
docker compose up --build -d --wait
powershell -File incident-response/start.ps1
curl.exe http://localhost:8001/healthz
```

Alternatively run `uv run --frozen python incident-response/server.py` in a
terminal. The service binds only to `127.0.0.1:8001`; do not expose it publicly.
`start.ps1` starts a hidden process and saves its PID in `runtime/server.pid`.
To stop it, verify that PID belongs to this responder before stopping the process.

`POST /alerts` accepts a Grafana JSON `alerts` array with per-alert `status`,
`labels`, `annotations`, and optional `startsAt`. It returns HTTP 202 with a
`result_url`; poll that URL with GET for the captured `response` and `last_line`.
Resolved-only deliveries do not invoke the agent. One process runs at a time;
concurrent distinct requests receive 429. Duplicate sanitized alert groups are
deduplicated on disk, including after failure or restart. Include `startsAt` to
distinguish subsequent real incidents. Incomplete jobs are never replayed.

The exact homework notification is:

```powershell
curl.exe -X POST http://localhost:8001/alerts `
  -H "Content-Type: application/json" `
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"ResponderTest","test":"true"},"annotations":{"summary":"Test notification; no incident to fix"}}]}'
```

Use PowerShell 7.3+ native argument passing for literal JSON strings. Windows
PowerShell 5.1 can strip JSON quotes when invoking native executables.

## Context and permissions

Only explicit known-safe label/annotation values are retained. Unknown fields
and free text are dropped, even under familiar keys. To support another alert,
review and extend `context.py`'s allowlist. Non-test alerts collect the preceding
five minutes of lookup counter increases, up to 20 5xx log records' technical
metadata, and at most three linked Tempo traces with route/status details. From
trace exception events it retains only allowlisted exception class names and
three exact calendar diagnostics (`day is out of range for month`, `month must
be in 1..12`, and `year out of range`).
Backend URLs and queries are fixed; incoming URLs are never fetched. Missing
telemetry is recorded as unavailable. Test-marked alerts skip telemetry entirely.

No raw alert payload, credentials, authorization/cookie headers, log body,
request/response body, order ID, customer contents, exception stack trace,
unmatched exception messages, or arbitrary resource fields are stored. The only
source read is `app/main.py`: top-level `as_dict`, `order_detail`, and `get_order`
functions plus a `datetime` import containing only `datetime`, `timedelta`, or
`timezone`. Backend responses are bounded and only processed in memory. Artifacts under `incidents/<id>/` contain sanitized
context, execution state, and the agent's textual answer; they are git-ignored.
Raw CLI stdout/stderr diagnostics are not saved. The successful JSON result's
text is captured without rewriting its final line.

The agent receives sanitized JSON on stdin in an empty temporary directory.
`--tools ""`, `--safe-mode`, `--strict-mcp-config`, `--permission-mode dontAsk`,
and `--no-session-persistence` disable tools, customizations, MCP and transcripts.
The native CLI uses its existing login; the responder neither reads nor copies
credentials. Arbitrary project/API-key environment variables are not inherited.
There is one CLI invocation, a 180-second timeout, no automatic retry, no
fallback agent, and no remediation capability. Failed runs return a generic
error instead of potentially sensitive CLI diagnostics.

Run `uv run --frozen pytest -q`. Tests mock the agent and telemetry; they never
launch a real agent. The live homework test is the only real invocation.

Question 5 uses a manually delivered synthetic notification. Question 6 adds
`observability/grafana/provisioning/alerting/incident-responder.json`: a webhook
contact point at `http://host.docker.internal:8001/alerts` and a notification
policy matching only the Order Tracker `Order lookup HTTP 5xx` alert. The
existing default email route is preserved for unmatched alerts. Initial group
wait is 10 seconds, group interval 30 seconds, repeat interval 4 hours.

Start the host responder before restarting Grafana to load provisioning:
`docker compose restart grafana`. Confirm host reachability from the container
using the responder health endpoint. Grafana delivers actual firing/resolved
notifications; repeated deliveries reuse the stored incident and resolved
notifications do not launch the agent. No manual POST substitutes for Grafana.

The agent still has no filesystem or execution tools. It receives only the
allowlisted source snippets and can suggest a unified diff as text; the responder
does not apply patches. Review the captured proposal and approve any application
remediation separately.

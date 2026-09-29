"""Loopback-only incident intake with durable deduplication and no automatic replay."""
import hashlib
import json
from pathlib import Path
from threading import Lock

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from agent import run_agent
from context import collect_context, parse_alerts

STORE = Path(__file__).resolve().parent / "incidents"
lock = Lock()
busy = Lock()
app = FastAPI(title="Local incident responder", docs_url=None, redoc_url=None)


def save(path, result):
    temporary = path / "result.tmp"
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary.replace(path / "result.json")


def investigate(path, alerts):
    try:
        context = collect_context(alerts)
        (path / "context.json").write_text(json.dumps(context, indent=2), encoding="utf-8")
        save(path, {"status": "running", "attempts": 1})
        result = run_agent(context)
        save(path, result)
        if result.get("status") == "completed":
            (path / "response.txt").write_text(result["response"], encoding="utf-8")
    except Exception:
        save(path, {"status": "failed", "error": "Investigation failed; no retry"})
    finally:
        busy.release()


@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "context-only", "automatic_retries": False}


@app.post("/alerts", status_code=202)
async def alerts(request: Request, tasks: BackgroundTasks):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise HTTPException(415, "Expected application/json")
    if request.headers.get("origin"):
        raise HTTPException(403, "Browser-origin requests are not accepted")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, "Payload too large")
    try:
        cleaned = parse_alerts(json.loads(body))
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid Grafana alert payload") from None
    if not cleaned:
        return {"status": "ignored", "reason": "No firing alerts"}
    incident_id = hashlib.sha256(json.dumps(cleaned, sort_keys=True).encode()).hexdigest()
    path = STORE / incident_id
    with lock:
        if path.exists():
            return {"incident_id": incident_id, "status": "duplicate", "result_url": f"/incidents/{incident_id}"}
        if not busy.acquire(blocking=False):
            raise HTTPException(429, "An investigation is already running")
        try:
            path.mkdir(parents=True)
            save(path, {"status": "accepted", "attempts": 0})
        except Exception:
            busy.release()
            raise HTTPException(503, "Incident store unavailable") from None
        tasks.add_task(investigate, path, cleaned)
    return {"incident_id": incident_id, "status": "accepted", "result_url": f"/incidents/{incident_id}"}


@app.get("/incidents/{incident_id}")
def incident(incident_id: str):
    if len(incident_id) != 64 or any(c not in "0123456789abcdef" for c in incident_id):
        raise HTTPException(404, "Unknown incident")
    path = STORE / incident_id / "result.json"
    if not path.is_file():
        raise HTTPException(404, "Unknown incident")
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001, access_log=False)

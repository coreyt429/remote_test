# /home/ubuntu/api/app.py
from __future__ import annotations

from flask import Flask, request, jsonify
from celery.result import AsyncResult

# If your Celery app object is exposed as `celery_app`:
from celery_app import celery_app as celery
# If instead you export `celery` directly, use:
# from celery_app import celery

app = Flask(__name__)

@app.get("/")
def version() -> tuple[dict, int]:
    return jsonify({"vetest": 0.02}), 200


@app.post("/task")
def enqueue_task():
    """
    JSON body formats supported:

    1) Simple single-arg call (most convenient):
       {
         "task_name": "dns.check_records",
         "data": { ... }           # passed as the single positional arg
       }

    2) Explicit args/kwargs (advanced):
       {
         "task_name": "dns.check_records",
         "data": {
           "args": [ {...} ],
           "kwargs": { "timeout": 3.0, "include_text": true }
         }
       }
    """
    payload = request.get_json(silent=True, force=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    task_name = payload.get("task_name")
    data = payload.get("data")

    if not task_name:
        return jsonify({"error": "Missing 'task_name'"}), 400

    # Normalize args/kwargs
    args, kwargs = [], {}
    if isinstance(data, dict) and ("args" in data or "kwargs" in data):
        args = data.get("args", []) or []
        kwargs = data.get("kwargs", {}) or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return jsonify({"error": "'data.args' must be a list and 'data.kwargs' must be an object"}), 400
    elif data is not None:
        # Pass `data` as a single positional argument
        args = [data]

    try:
        # Generic dispatch: send by dotted task name registered in your worker
        # e.g., "dns.check_records" (from tasks.py)
        async_result = celery.send_task(task_name, args=args, kwargs=kwargs)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": f"Failed to enqueue task '{task_name}': {exc}"}), 400

    return jsonify({"task_id": async_result.id}), 202


@app.get("/task/<task_id>")
def task_status(task_id: str):
    res = AsyncResult(task_id, app=celery)

    # Standard fields
    payload = {
        "task_id": task_id,
        "state": res.state,              # PENDING / STARTED / RETRY / FAILURE / SUCCESS
        "ready": res.ready(),
    }

    # Status-specific enrichments
    if res.failed():
        # res.info may be an Exception; str() to serialize
        payload["error"] = str(res.info)
        return jsonify(payload), 200

    if res.successful():
        payload["result"] = res.result
        return jsonify(payload), 200

    # For PENDING/STARTED/RETRY we just return the state
    return jsonify(payload), 200


if __name__ == "__main__":
    # In production you’re already running this under Gunicorn.
    app.run(host="0.0.0.0", port=8000)


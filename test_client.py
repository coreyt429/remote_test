#!/usr/bin/env python3
import time
import sys
import requests

BASE_URL = "http://vetest01.mymtm.us"
POST_TASK_URL = f"{BASE_URL}/task"  # POST here
GET_TASK_URL = f"{BASE_URL}/task/{{task_id}}"  # GET here

# sample payload (your example)
dns_data = {
    "akixi.altvoip.com": {
        "original": ["208.67.15.19"],
        "13056": ["208.67.15.19", "208.67.15.18"],
        "13057": ["208.67.15.18"],
    },
    "akixi.mymtm.us": {
        "original": ["208.67.14.41", "208.67.15.41", "208.67.15.42"],
        "13056": [
            "208.67.14.41",
            "208.67.15.41",
            "208.67.15.42",
            "208.67.14.42",
            "208.67.15.39",
            "208.67.15.38",
        ],
        "13057": ["208.67.14.42", "208.67.15.39", "208.67.15.38"],
    },
}


def submit_task(session: requests.Session, data: dict) -> str:
    """Submit the DNS check task and return task_id."""
    payload = {
        "task_name": "dns.check_records",
        # pass dns_data as the single positional arg; kwargs are optional
        "data": {
            "args": [data],
            "kwargs": {"timeout": 3.0, "include_text": True},
        },
    }
    r = session.post(POST_TASK_URL, json=payload, timeout=10)
    r.raise_for_status()
    resp = r.json()
    task_id = resp.get("task_id")
    if not task_id:
        raise RuntimeError(f"Unexpected response from server: {resp}")
    return task_id


def wait_for_task(
    session: requests.Session,
    task_id: str,
    timeout_s: int = 120,
    poll_interval: float = 1.0,
) -> dict:
    """Poll the task status until finished or timeout. Returns final JSON payload."""
    deadline = time.time() + timeout_s
    last_state = None
    while time.time() < deadline:
        r = session.get(GET_TASK_URL.format(task_id=task_id), timeout=10)
        r.raise_for_status()
        data = r.json()
        state = data.get("state")
        if state != last_state:
            print(f"[status] {state}", flush=True)
            last_state = state
        if state in {"SUCCESS", "FAILURE"}:
            return data
        time.sleep(poll_interval)
    raise TimeoutError(f"Task {task_id} did not finish within {timeout_s}s")


def print_result(payload: dict) -> None:
    """Pretty-print SUCCESS/FAILURE result from the API."""
    state = payload.get("state")
    if state == "FAILURE":
        print("\nTask failed:")
        print(payload.get("error", "Unknown error"))
        return

    result = payload.get("result", {})
    summary = result.get("summary", {})
    results = result.get("results", {})

    print(
        f"\nSummary: domains={summary.get('domains')} duration={summary.get('duration_sec')}s"
    )
    for domain, info in results.items():
        # If task was run with include_text=True, server includes a 'text' string per domain
        line = info.get("text")
        if line:
            print(line)
        else:
            # fallback if text omitted
            print(
                f"{domain}: state={info.get('state')} retrieved={info.get('retrieved')}"
            )


def main() -> int:
    with requests.Session() as sess:
        try:
            task_id = submit_task(sess, dns_data)
            print(f"Enqueued task: {task_id}")
            payload = wait_for_task(sess, task_id, timeout_s=180, poll_interval=1.0)
            print_result(payload)
            return 0
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

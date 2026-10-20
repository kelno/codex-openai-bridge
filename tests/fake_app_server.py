"""Minimal stand-in for `codex app-server` used by the tests.

Speaks newline-delimited JSON-RPC on stdio. Each turn answers with
"echo:<input text>" (or {"echo": "<input text>"} when an outputSchema is given)
streamed in a few deltas, so concurrent turns interleave. Every request's
params are appended to $FAKE_APP_SERVER_LOG as JSON lines.

Input text containing FAIL makes the turn fail with a usage-limit error, and
NOUSAGE skips the token usage notification.
"""

import json
import os
import sys
import threading
import time
import uuid

_out_lock = threading.Lock()
_log_path = os.environ.get("FAKE_APP_SERVER_LOG")
_delay = float(os.environ.get("FAKE_APP_SERVER_DELAY", "0.05"))


def send(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def log(method, params):
    if _log_path:
        with _out_lock, open(_log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"method": method, "params": params}) + "\n")


def run_turn(thread_id, turn_id, params):
    text = "".join(i.get("text", "") for i in params.get("input", []) if i.get("type") == "text")
    send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id}}})
    if "FAIL" in text:
        send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
            "id": turn_id, "status": "failed",
            "error": {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}}}})
        return
    answer = json.dumps({"echo": text}) if params.get("outputSchema") else f"echo:{text}"
    item_id = f"msg_{uuid.uuid4().hex[:6]}"
    step = max(1, len(answer) // 4)
    for i in range(0, len(answer), step):
        time.sleep(_delay)
        send({"method": "item/agentMessage/delta", "params": {
            "threadId": thread_id, "turnId": turn_id, "itemId": item_id, "delta": answer[i:i + step]}})
    send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
          "item": {"type": "agentMessage", "id": item_id, "text": answer}}})
    if "NOUSAGE" not in text:
        send_usage(thread_id, turn_id, text, answer)
    send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}}})


def send_usage(thread_id, turn_id, text, answer):
    send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id, "tokenUsage": {
        "last": {"inputTokens": 100 + len(text), "cachedInputTokens": 40, "outputTokens": len(answer),
                 "reasoningOutputTokens": 3, "totalTokens": 100 + len(text) + len(answer)},
        "total": {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0,
                  "reasoningOutputTokens": 0, "totalTokens": 0}}}})


def main():
    threads = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        params = msg.get("params") or {}
        rid = msg.get("id")
        if method:
            log(method, params)
        if rid is None:
            continue  # notification (e.g. "initialized")
        if method == "initialize":
            send({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "model/list":
            send({"id": rid, "result": {"data": [
                {"id": "gpt-5", "hidden": False,
                 "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}]},
                {"id": "gpt-secret", "hidden": True, "supportedReasoningEfforts": []},
            ]}})
        elif method == "thread/start":
            thread_id = str(uuid.uuid4())
            threads[thread_id] = params
            send({"id": rid, "result": {"thread": {"id": thread_id}, "model": params.get("model") or "gpt-5"}})
        elif method == "turn/start":
            turn_id = str(uuid.uuid4())
            send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
            threading.Thread(target=run_turn, args=(params["threadId"], turn_id, params), daemon=True).start()
        else:
            send({"id": rid, "result": {}})


if __name__ == "__main__":
    main()

"""Minimal stand-in for `codex app-server` used by the tests.

Speaks newline-delimited JSON-RPC on stdio. Each turn answers with
"echo:<input text>" (or {"echo": "<input text>"} when an outputSchema is given)
streamed in a few deltas, so concurrent turns interleave. Every request's
params are appended to $FAKE_APP_SERVER_LOG as JSON lines.

Input text containing FAIL makes the turn fail with a usage-limit error,
BADREQ makes it fail with an OpenAI invalid_request_error, and NOUSAGE skips the
token usage notification. Like OpenAI's strict mode, an outputSchema whose
objects do not list every property in `required` is rejected.
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


def strict_schema_problem(schema, path="schema"):
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            missing = [k for k in props if k not in (schema.get("required") or [])]
            if missing:
                return f"'{path}.required' is required to include every key in properties. Missing '{missing[0]}'."
            for key, sub in props.items():
                problem = strict_schema_problem(sub, f"{path}.properties.{key}")
                if problem:
                    return problem
        if "items" in schema:
            return strict_schema_problem(schema["items"], f"{path}.items")
    return None


def fail_openai(thread_id, turn_id, message, code):
    inner = {"type": "error", "status": 400, "error": {
        "message": message, "type": "invalid_request_error", "param": "text.format.schema", "code": code}}
    error = {"message": json.dumps(inner), "codexErrorInfo": "other"}
    send({"method": "error", "params": {"threadId": thread_id, "turnId": turn_id, "error": error, "willRetry": False}})
    send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
        "id": turn_id, "status": "failed", "error": error}}})


def run_turn(thread_id, turn_id, params):
    text = "".join(i.get("text", "") for i in params.get("input", []) if i.get("type") == "text")
    send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id}}})
    problem = strict_schema_problem(params["outputSchema"]) if params.get("outputSchema") else None
    if problem:
        fail_openai(thread_id, turn_id, f"Invalid schema for response_format 'codex_output_schema': {problem}",
                    "invalid_json_schema")
        return
    if "BADREQ" in text:
        fail_openai(thread_id, turn_id, "Something about the request is invalid.", "invalid_value")
        return
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
        if rid is None:
            if method:
                log(method, params)
            continue  # notification (e.g. "initialized")
        if method == "thread/start":
            thread_id = str(uuid.uuid4())
            log(method, {**params, "_threadId": thread_id})
        elif method:
            log(method, params)
        if method == "initialize":
            send({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "model/list":
            send({"id": rid, "result": {"data": [
                {"id": "gpt-5", "hidden": False,
                 "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}]},
                {"id": "gpt-secret", "hidden": True, "supportedReasoningEfforts": []},
            ]}})
        elif method == "thread/start":
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

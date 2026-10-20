"""Measure time-to-first-token and total latency of the wrapper.

Stdlib only. Examples:

    python scripts/bench_latency.py                       # default suite
    python scripts/bench_latency.py --runs 5 --concurrency 8
    python scripts/bench_latency.py --base http://127.0.0.1:8005/v1 --key sk-...

The default suite reproduces the reported numbers: non-streamed and streamed
calls for "gpt-5.6-luna" and "gpt-5.6-luna low", a tiny prompt and a ~3k-token
prompt, a strict json_schema request, GET /models, and one burst of
concurrent requests.
"""

import argparse
import http.client
import json
import statistics
import sys
import threading
import time
import urllib.parse
from typing import Optional

SMALL_PROMPT = "Count from 1 to 40, separated by spaces."
# ~3k tokens of filler followed by a trivial instruction.
LARGE_PROMPT = (
    "Background notes (ignore):\n"
    + ("The quick brown fox jumps over the lazy dog near the riverbank. " * 230)
    + "\n\nSay hi."
)
JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "npc_line",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "speaker": {"type": "string"},
                "line": {"type": "string"},
                "mood": {"type": "string", "enum": ["happy", "angry", "neutral"]},
            },
            "required": ["speaker", "line", "mood"],
            "additionalProperties": False,
        },
    },
}


class Client:
    def __init__(self, base: str, key: Optional[str], timeout: float) -> None:
        parsed = urllib.parse.urlparse(base)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.https = parsed.scheme == "https"
        self.prefix = parsed.path.rstrip("/")
        self.key = key
        self.timeout = timeout

    def _conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    def models(self) -> dict:
        t0 = time.perf_counter()
        conn = self._conn()
        conn.request("GET", f"{self.prefix}/models", headers=self._headers())
        resp = conn.getresponse()
        body = resp.read()
        total = time.perf_counter() - t0
        conn.close()
        count = len(json.loads(body).get("data", [])) if resp.status == 200 else 0
        return {"status": resp.status, "total": total, "ttft": total, "count": count}

    def chat(self, model: str, prompt: str, stream: bool, **extra) -> dict:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
            **extra,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        t0 = time.perf_counter()
        conn = self._conn()
        conn.request(
            "POST",
            f"{self.prefix}/chat/completions",
            body=json.dumps(payload),
            headers=self._headers(),
        )
        resp = conn.getresponse()
        result = {"status": resp.status, "ttft": None, "id": None, "usage": "absent"}
        if resp.status != 200:
            result["error"] = resp.read().decode(errors="replace")[:300]
            result["total"] = time.perf_counter() - t0
            conn.close()
            return result

        if not stream:
            data = json.loads(resp.read())
            result["total"] = time.perf_counter() - t0
            result["ttft"] = result["total"]
            result["id"] = data.get("id")
            result["usage"] = data.get("usage", "absent")
            result["text"] = data["choices"][0]["message"]["content"]
            conn.close()
            return result

        parts = []
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]":
                break
            data = json.loads(chunk)
            result["id"] = result["id"] or data.get("id")
            if data.get("usage"):
                result["usage"] = data["usage"]
            for choice in data.get("choices") or []:
                content = (choice.get("delta") or {}).get("content")
                if content:
                    if result["ttft"] is None:
                        result["ttft"] = time.perf_counter() - t0
                    parts.append(content)
        result["total"] = time.perf_counter() - t0
        result["text"] = "".join(parts)
        conn.close()
        return result


def _fmt(values) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "   n/a"
    if len(values) == 1:
        return f"{values[0]:6.2f}"
    return f"{statistics.median(values):6.2f} (min {min(values):.2f}, max {max(values):.2f})"


def run_case(label: str, fn, runs: int, verbose: bool) -> list:
    results = []
    for _ in range(runs):
        r = fn()
        results.append(r)
        if r["status"] != 200:
            print(f"  {label}: HTTP {r['status']} {r.get('error', '')}")
            break
    ok = [r for r in results if r["status"] == 200]
    if ok:
        last = ok[-1]
        print(
            f"{label:<38} ttft {_fmt([r['ttft'] for r in ok])}  "
            f"total {_fmt([r['total'] for r in ok])}  "
            f"id={last.get('id')!s:.28} usage={json.dumps(last.get('usage'))}"
        )
        if verbose and "text" in last:
            print("    text:", repr(last["text"][:160]))
    return results


def run_burst(client: Client, model: str, n: int, stream: bool) -> None:
    results: list = [None] * n
    prompts = [f"Reply with exactly the number {i * 7 + 3} and nothing else." for i in range(n)]

    def worker(i: int) -> None:
        results[i] = client.chat(model, prompts[i], stream)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    ok = [r for r in results if r and r["status"] == 200]
    mixed = [
        i for i, r in enumerate(results)
        if r and r["status"] == 200 and str(i * 7 + 3) not in r.get("text", "")
    ]
    ids = {r.get("id") for r in ok}
    print(
        f"{'burst x' + str(n) + (' stream' if stream else ''):<38} ttft {_fmt([r['ttft'] for r in ok])}  "
        f"total {_fmt([r['total'] for r in ok])}  wall {wall:.2f}s  ok {len(ok)}/{n}  "
        f"unique ids {len(ids)}  wrong answers {mixed or 'none'}"
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8005/v1")
    ap.add_argument("--key", default=None, help="PROXY_API_KEY, if the wrapper requires one")
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--quick", action="store_true", help="only the small-prompt cases")
    ap.add_argument("-v", "--verbose", action="store_true", help="print answer text")
    args = ap.parse_args()

    c = Client(args.base, args.key, args.timeout)
    m, ml = args.model, f"{args.model} low"
    print(f"target {args.base}  runs/case {args.runs}  (seconds)\n")

    run_case("GET /models", c.models, args.runs, False)
    run_case(f"{m} small", lambda: c.chat(m, SMALL_PROMPT, False), args.runs, args.verbose)
    run_case(f"{m} small stream", lambda: c.chat(m, SMALL_PROMPT, True), args.runs, args.verbose)
    run_case(f"{ml} small", lambda: c.chat(ml, SMALL_PROMPT, False), args.runs, args.verbose)
    run_case(f"{ml} small stream", lambda: c.chat(ml, SMALL_PROMPT, True), args.runs, args.verbose)
    if not args.quick:
        run_case(f"{m} ~3k prompt", lambda: c.chat(m, LARGE_PROMPT, False), args.runs, args.verbose)
        run_case(
            f"{ml} json_schema",
            lambda: c.chat(ml, "Invent one line of dialogue for a grumpy blacksmith NPC.", False,
                           response_format=JSON_SCHEMA),
            args.runs,
            True,
        )
        run_case(
            f"{ml} json_schema stream",
            lambda: c.chat(ml, "Invent one line of dialogue for a cheerful baker NPC.", True,
                           response_format=JSON_SCHEMA),
            1,
            True,
        )
        if args.concurrency > 1:
            run_burst(c, ml, args.concurrency, stream=False)
            run_burst(c, ml, args.concurrency, stream=True)


if __name__ == "__main__":
    main()

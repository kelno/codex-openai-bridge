# codex-openai-bridge

A FastAPI server that puts an OpenAI-compatible API in front of the [Codex CLI](https://github.com/openai/codex), so any OpenAI client can use a ChatGPT/Codex sign-in (or a Codex API-key login) by changing `base_url`. It needs a Codex CLI recent enough to have `codex app-server` (tested with 0.163).

- `GET /v1/models`
- `POST /v1/chat/completions` (streaming and non-streaming, `response_format` JSON schemas, images, token usage)
- `POST /v1/responses` (minimal subset)

## How requests are served

The server keeps one `codex app-server` process running and gives every request its own short-lived (ephemeral) Codex thread on it. Requests run in parallel (up to `CODEX_MAX_PARALLEL_REQUESTS`, default 8) and their streams never mix. A thread's system prompt is fixed when it starts, but the model and effort are chosen per turn, so the server keeps a few pre-started spare threads for each recently used system prompt (`CODEX_IDLE_THREADS`, default 2). A request whose system prompt has a spare skips thread start and the per-thread setup Codex does before its first turn, which saves about 0.6–1 s. Spares are never reused: each one serves exactly one request and is then replaced in the background.

The app-server is started with a lean profile: plugins, MCP servers, hooks, shell/browser/computer-use tools, skills and the injected sandbox/environment instructions are turned off, and the client's `system` message replaces Codex's agent prompt. A tiny request costs about 2.3k input tokens instead of about 13k.

Measured on Windows with `gpt-5.6-luna` (ChatGPT sign-in), using `scripts/bench_latency.py`:

| | `codex exec` per request (old) | `codex app-server` | app-server + spare threads (now) |
|---|---|---|---|
| small prompt, total | 4.6–5.4 s | 2.0–2.7 s | 1.4–2.0 s |
| streamed, first token | 4.1–4.5 s | 1.9–2.5 s | 1.0–1.3 s |
| 8 concurrent requests, wall | 20–27 s | 2.4–3.5 s | 2.3–2.8 s |
| `response_format: json_schema` | ignored | enforced |
| `usage` | zeros | real token counts |

What is left is the model backend's own time to first token.

## Quick start

1. Install [uv](https://docs.astral.sh/uv/) and the Codex CLI (`npm i -g @openai/codex`, or the desktop app's bundled CLI).
2. Sign in to Codex as the same OS user that runs this server:

   ```bash
   codex login                     # ChatGPT sign-in
   codex login --api-key "sk-..."  # or an OpenAI API key
   ```

3. Install dependencies and run:

   ```bash
   uv sync
   cp .env.example .env            # optional, see docs/ENV.md
   uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

4. Point a client at it:

   ```python
   from openai import OpenAI

   client = OpenAI(base_url="http://localhost:8000/v1", api_key="PROXY_API_KEY or anything")
   resp = client.chat.completions.create(
       model="gpt-5.6-luna low",
       messages=[{"role": "user", "content": "Say hi"}],
   )
   print(resp.choices[0].message.content, resp.usage)
   ```

## API

### Models

`GET /v1/models` lists the models Codex reports for your account (`model/list`), each followed by one alias per reasoning effort the model supports, for example `gpt-5.6-luna low`. Omitting `model` in a request uses the model set in Codex's `config.toml`.

### Chat completions

Accepted request fields:

- `model`, `messages` (`system`/`developer`/`user`/`assistant`/`tool`; text and image parts)
- `stream`, `stream_options.include_usage`
- `reasoning_effort` (`none`/`minimal` map to the lowest effort the model supports)
- `response_format`: `{"type": "json_schema", "json_schema": {"name", "schema", "strict"}}` constrains the answer to the schema; `{"type": "json_object"}` asks for a JSON object
- `temperature`, `max_tokens`: accepted and ignored

Responses carry a unique `id` (`chatcmpl-...`), `created`, `model` and, when the backend reports it, `usage` with `prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens`. If usage is unavailable the field is omitted rather than zero-filled. Streams follow the OpenAI chunk format (role chunk, content deltas, a final `finish_reason: "stop"` chunk, an optional usage chunk, then `data: [DONE]`).

How messages map to Codex: `system`/`developer` messages become the thread's base instructions. A single user message is sent as is. A longer conversation is sent as a `User:` / `Assistant:` transcript.

Errors use FastAPI's format, `{"detail": {"message", "type", "code"}}`, with 401/429/504 passed through from Codex where they apply. Errors that happen before the first token of a stream are returned as HTTP errors too.

### Responses API

`POST /v1/responses` accepts `model`, `input` (string, content parts, or role/content messages), `instructions`, `reasoning.effort` and `stream`. Streaming emits `response.created`, `response.output_text.delta`, `response.output_text.done` and `response.completed` events.

## Configuration

All settings are environment variables (a `.env` file is loaded automatically). The full reference is [docs/ENV.md](docs/ENV.md). The most relevant ones:

| Variable | Default | Purpose |
|---|---|---|
| `CODEX_LEAN` | `true` | Disable plugins, MCP servers, hooks, tools and injected instructions in the app-server |
| `CODEX_MAX_PARALLEL_REQUESTS` | `8` | Concurrent Codex turns |
| `CODEX_IDLE_THREADS` | `2` | Pre-started spare threads per system prompt (`0` disables) |
| `CODEX_TIMEOUT` | `120` | Seconds per request |
| `PROXY_API_KEY` | unset | Require `Authorization: Bearer <key>` |
| `RATE_LIMIT_PER_MINUTE` | `0` (off) | Requests per minute per client IP |
| `CODEX_SANDBOX_MODE` | `read-only` | Sandbox for anything Codex executes |
| `CODEX_HOME` | `~/.codex` | Codex home (read by Codex itself) |

### The Codex home and `AGENTS.md`

The wrapper uses your Codex home (`$CODEX_HOME`, default `~/.codex`) for auth and `config.toml`. Codex adds `$CODEX_HOME/AGENTS.md` to every request as user instructions, and there is no setting to skip it. The server logs a warning at startup if that file is not empty. Either empty it, or give the wrapper its own home: set `CODEX_HOME=<dir>` for the server and run `CODEX_HOME=<dir> codex login` once.

## Logs and benchmark

Each request logs its phases, for example:

```
[b46e2a61] codex model=gpt-5.6-luna effort=low slot=0ms thread=0ms (spare) turn=5ms first_token=1131ms total=1588ms in=2350 cached=0 out=83
```

`scripts/bench_latency.py` measures time to first token and total time for streamed and non-streamed calls, a ~3k-token prompt, strict JSON schemas, `GET /models` and a burst of concurrent requests:

```bash
uv run python scripts/bench_latency.py --base http://127.0.0.1:8000/v1 --runs 3
```

## Docker

```bash
docker compose build
docker compose run --rm codex-openai-bridge codex login   # once, stores auth in the mounted ~/.codex
docker compose up
```

The compose file publishes the API on `localhost:8020` and mounts `${HOME}/.codex` into the container.

## Security notes

- Codex threads always run with `approvalPolicy=never` and the configured sandbox (`read-only` by default). In the lean profile the model has no shell or tools at all.
- Image inputs must be `data:` URIs (written to a temporary file in the work directory, deleted after the request) or http(s) URLs (fetched by Codex). `file://` URLs and paths are rejected, so clients cannot make the server read local files.
- `PROXY_API_KEY` protects this API; it is unrelated to Codex's own login.

## Development

```bash
uv sync
uv run pytest
```

Tests use `tests/fake_app_server.py`, a stand-in for `codex app-server` that speaks the same JSON-RPC protocol, so they never call a real model.

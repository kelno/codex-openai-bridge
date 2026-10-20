# Environment configuration

The server reads its settings from environment variables. A `.env` file in the repository root is loaded automatically (via `pydantic-settings`); real environment variables take precedence, and unknown keys (such as docker compose's `HOST_UID`) are ignored. To load a different file, set `CODEX_ENV_FILE` (for example `.env.local`) in the OS environment before starting the server.

| Variable | Default | Meaning |
|---|---|---|
| `PROXY_API_KEY` | unset | If set, every request must send `Authorization: Bearer <PROXY_API_KEY>`. |
| `RATE_LIMIT_PER_MINUTE` | `0` | Requests per minute per client IP; `0` disables the limit. |
| `CODEX_PATH` | `codex` | Codex binary (absolute path, or a name looked up on `PATH`). |
| `CODEX_WORKDIR` | see below | Working directory of Codex threads; also holds temporary image files. Created if missing. |
| `CODEX_SANDBOX_MODE` | `read-only` | `read-only`, `workspace-write` or `danger-full-access`. With the lean profile the model has no tools, so this only matters when `CODEX_LEAN=false`. |
| `CODEX_TIMEOUT` | `120` | Seconds before a request is aborted with HTTP 504. |
| `CODEX_MAX_PARALLEL_REQUESTS` | `8` | Maximum concurrent Codex turns; further requests wait for a free slot. |
| `CODEX_IDLE_THREADS` | `8` | Pre-started spare threads kept per system prompt, so requests skip thread start and Codex's per-thread setup (saves ~0.6–1 s). The default matches `CODEX_MAX_PARALLEL_REQUESTS`, so a full burst with one system prompt finds spares; lower it to save a few MB. Spares exist for at most 8 recently used system prompts and are discarded after 10 minutes unused. Each serves one request only. `0` disables the pool. |
| `CODEX_LEAN` | `true` | Start the app-server with plugins, apps, hooks, multi-agent, shell/browser/computer-use tools, image generation, skills, web search, the `notify` hook and every configured MCP server turned off, and without Codex's sandbox-permission and environment-context instructions. Set `false` to expose the full Codex agent. |
| `CODEX_HOME` | `~/.codex` | Codex's own variable: where `auth.json`, `config.toml` and `AGENTS.md` live. The server passes it through to Codex. |

`CODEX_WORKDIR` defaults to `/workspace` on Linux/macOS (the Docker layout) and `%LOCALAPPDATA%\codex-openai-bridge\workspace` on Windows. On Windows use an absolute path with a drive letter, because `/workspace` resolves to the root of the current drive.

## Codex credentials

Codex handles authentication itself: run `codex login` (ChatGPT sign-in) or `codex login --api-key ...` as the user that runs the server, with the same `CODEX_HOME`.

## `AGENTS.md` in the Codex home

Codex adds `$CODEX_HOME/AGENTS.md` (or `AGENTS.override.md`) to every request as user instructions, and has no setting to skip it. The server logs a warning at startup if either file is non-empty. Empty it if that Codex home only serves the wrapper, or give the wrapper a separate `CODEX_HOME`.

"""Codex backend: one persistent ``codex app-server`` process.

The app-server speaks JSON-RPC over stdio. Every request runs on its own
ephemeral thread, so concurrent requests execute in parallel and their
notifications are routed by thread id. Compared with spawning ``codex exec``
per request, this removes process start, config/plugin/MCP loading and the
shutdown work after every turn.

Security notes: threads always start with ``approvalPolicy="never"`` and the
configured sandbox mode (``read-only`` by default). The lean profile also
disables the shell, plugins, MCP servers and hooks, so the model has no way to
run commands. Server-initiated requests (approvals, elicitations) are answered
with an error, which denies them.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import tomllib

from .config import codex_home, settings

logger = logging.getLogger(__name__)

_STREAM_LIMIT = 64 * 1024 * 1024  # turn/completed can carry the whole turn
_REQUEST_TIMEOUT = 60.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

DEFAULT_INSTRUCTIONS = "You are a helpful assistant."

# Features turned off in the lean profile. Every one of them adds tools,
# instructions or startup work that a plain chat completion does not need.
LEAN_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "realtime_conversation",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "tool_suggest",
    "unified_exec",
    "view_image",
    "workspace_dependencies",
)

LEAN_CONFIG_OVERRIDES = (
    "notify=[]",
    "project_doc_max_bytes=0",
    "include_permissions_instructions=false",
    "include_environment_context=false",
    "skills.include_instructions=false",
    'web_search="disabled"',
)

# Notifications the wrapper never reads; opting out keeps stdout quiet.
_OPT_OUT_NOTIFICATIONS = [
    "account/rateLimits/updated",
    "account/updated",
    "mcpServer/startupStatus/updated",
    "remoteControl/status/changed",
    "thread/status/changed",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
]


class CodexError(Exception):
    """A Codex failure, with the HTTP status the API should report."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class TurnResult:
    """Filled in while a turn runs; read by the caller once it finishes."""

    text: str = ""
    model: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    timings: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    efforts: Tuple[str, ...] = ()
    hidden: bool = False


# --- process setup ------------------------------------------------------------


def _resolve_codex_executable() -> str:
    exe = settings.codex_path
    found = exe if os.path.isabs(exe) and os.path.isfile(exe) else shutil.which(exe)
    if not found:
        raise CodexError(f"codex binary not found (CODEX_PATH='{exe}')", status_code=500)
    return found


def _toml_key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def _configured_mcp_servers() -> List[str]:
    path = codex_home() / "config.toml"
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("Could not read %s to disable MCP servers: %s", path, exc)
        return []
    servers = data.get("mcp_servers")
    return list(servers) if isinstance(servers, dict) else []


def build_app_server_command() -> List[str]:
    """Return the ``codex app-server`` command line for the configured profile."""

    cmd = [_resolve_codex_executable(), "app-server", "--listen", "stdio://"]
    cmd += ["-c", f'sandbox_mode="{settings.sandbox_mode}"']
    if settings.lean:
        for feature in LEAN_DISABLED_FEATURES:
            cmd += ["--disable", feature]
        for override in LEAN_CONFIG_OVERRIDES:
            cmd += ["-c", override]
        for name in _configured_mcp_servers():
            cmd += ["-c", f"mcp_servers.{_toml_key(name)}.enabled=false"]
    return cmd


def warn_if_global_agents_md() -> None:
    """Warn when Codex's global AGENTS.md would be injected into every request.

    Codex adds ``$CODEX_HOME/AGENTS.md`` (or ``AGENTS.override.md``) to every
    thread as user instructions and has no setting to skip it, so a non-empty
    file costs tokens on each call and can steer answers away from the
    client's own system prompt.
    """

    for name in ("AGENTS.override.md", "AGENTS.md"):
        path = codex_home() / name
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Could not read %s: %s", path, exc)
            continue
        if content.strip():
            logger.warning(
                "%s is not empty (%d bytes). Codex adds it to every request as user "
                "instructions; empty it if this Codex home only serves the wrapper.",
                path,
                len(content.encode("utf-8")),
            )


def _turn_error_status(error: Dict[str, Any]) -> int:
    blob = json.dumps(error).lower()
    if "unauthorized" in blob or "401" in blob:
        return 401
    if "usagelimit" in blob or "usage_limit" in blob or "rate limit" in blob or "429" in blob:
        return 429
    return 502


class _Limiter:
    """Caps concurrent turns; the semaphore is created lazily in the running loop."""

    def __init__(self) -> None:
        self._sem: Optional[asyncio.Semaphore] = None

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, settings.max_parallel_requests))
        async with self._sem:
            yield

    def reset(self) -> None:
        self._sem = None


# --- client -------------------------------------------------------------------


class AppServerClient:
    """Multiplexes concurrent turns over one ``codex app-server`` process."""

    def __init__(self, command_factory: Callable[[], List[str]] = build_app_server_command) -> None:
        self._command_factory = command_factory
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._start_lock: Optional[asyncio.Lock] = None
        self._next_id = 0
        self._pending: Dict[Any, asyncio.Future] = {}
        self._threads: Dict[str, asyncio.Queue] = {}
        self._limiter = _Limiter()
        self.models: Dict[str, ModelInfo] = {}

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """Start the app-server if it is not running (also restarts it after a crash)."""

        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if not self.running:
                await self._spawn()

    async def _spawn(self) -> None:
        workdir = Path(settings.codex_workdir).expanduser()
        workdir.mkdir(parents=True, exist_ok=True)
        cmd = self._command_factory()
        t0 = time.perf_counter()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env={**os.environ, "NO_COLOR": "1"},
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            raise CodexError(f"Failed to launch codex app-server: {exc}", status_code=500) from exc
        self._reader_task = asyncio.create_task(self._read_stdout(self._proc))
        self._stderr_task = asyncio.create_task(self._read_stderr(self._proc))
        try:
            await self._request(
                "initialize",
                {
                    "clientInfo": {"name": "codex-wrapper", "title": "Codex Wrapper", "version": "0.2.0"},
                    "capabilities": {"optOutNotificationMethods": _OPT_OUT_NOTIFICATIONS},
                },
            )
            self._notify("initialized")
            self.models = await self._list_models()
        except Exception:
            await self.close()
            raise
        logger.info(
            "codex app-server ready in %.0f ms (pid %s, models: %s, lean=%s)",
            (time.perf_counter() - t0) * 1000,
            self._proc.pid,
            ", ".join(m.id for m in self.models.values() if not m.hidden) or "none",
            settings.lean,
        )

    async def _list_models(self) -> Dict[str, ModelInfo]:
        models: Dict[str, ModelInfo] = {}
        cursor = None
        while True:
            params: Dict[str, Any] = {"includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            page = await self._request("model/list", params)
            for m in page.get("data") or []:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                efforts = tuple(
                    e["reasoningEffort"]
                    for e in m.get("supportedReasoningEfforts") or []
                    if isinstance(e, dict) and e.get("reasoningEffort")
                )
                models[m["id"]] = ModelInfo(id=m["id"], efforts=efforts, hidden=bool(m.get("hidden")))
            cursor = page.get("nextCursor")
            if not cursor:
                return models

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        self._start_lock = None  # bound to the current event loop
        self._limiter.reset()
        if proc is None:
            return
        if proc.returncode is None:
            with suppress(Exception):
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
                with suppress(BaseException):
                    await task
        self._fail_all("codex app-server stopped")

    def _fail_all(self, message: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CodexError(message))
        self._pending.clear()
        for queue in self._threads.values():
            queue.put_nowait({"method": "_closed", "params": {"message": message}})

    async def _read_stdout(self, proc: asyncio.subprocess.Process) -> None:
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("app-server non-JSON output: %r", line[:200])
                    continue
                self._dispatch(msg)
        finally:
            if self._proc is proc:
                self._proc = None
            logger.warning("codex app-server exited (code %s)", proc.returncode)
            self._fail_all(f"codex app-server exited (code {proc.returncode})")

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = _ANSI_RE.sub("", line.decode("utf-8", errors="replace")).rstrip()
            if not text:
                continue
            if " ERROR " in text or " WARN " in text:
                logger.warning("app-server: %s", text)
            else:
                logger.debug("app-server: %s", text)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "id" in msg and "method" not in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(CodexError(f"app-server: {err.get('message', err)}"))
                else:
                    fut.set_result(msg.get("result") or {})
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if "id" in msg:
            # Server-initiated request (approval, elicitation, ...). Deny it.
            logger.warning("Denying app-server request %s", method)
            self._send({"id": msg["id"], "error": {"code": -32601, "message": "not supported by codex-wrapper"}})
            return
        queue = self._threads.get(params.get("threadId") or "")
        if queue is not None:
            queue.put_nowait(msg)
        elif method in ("configWarning", "deprecationNotice"):
            logger.warning("app-server %s: %s", method, params.get("summary") or params)

    def _send(self, obj: Dict[str, Any]) -> None:
        if not self.running:
            raise CodexError("codex app-server is not running")
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    async def _request(self, method: str, params: Dict[str, Any], timeout: float = _REQUEST_TIMEOUT) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            self._send({"id": request_id, "method": method, "params": params})
            await self._proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise CodexError(f"app-server {method} timed out", status_code=504)
        finally:
            self._pending.pop(request_id, None)

    def _forget(self, method: str, params: Dict[str, Any]) -> None:
        """Send a request whose response is not needed."""

        with suppress(Exception):
            self._send({"id": f"{method}-{uuid.uuid4().hex[:8]}", "method": method, "params": params})

    # --- turns -------------------------------------------------------------------

    def resolve_effort(self, model: Optional[str], effort: Optional[str]) -> Optional[str]:
        """Map an OpenAI-style effort onto one the model supports."""

        if not effort:
            return None
        effort = effort.strip().lower()
        info = self.models.get(model or "")
        supported = info.efforts if info else ()
        if not supported or effort in supported:
            return effort
        if effort in ("none", "minimal"):
            return supported[0]
        return effort

    async def run_turn(
        self,
        *,
        text: str,
        model: Optional[str] = None,
        instructions: Optional[str] = None,
        effort: Optional[str] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        images: Optional[List[str]] = None,
        result: Optional[TurnResult] = None,
        request_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Run one turn on a fresh ephemeral thread and yield answer text deltas.

        ``model=None`` uses the model configured in Codex's ``config.toml``.
        ``images`` are local file paths or http(s) URLs.
        """

        result = result if result is not None else TurnResult()
        request_id = request_id or uuid.uuid4().hex[:8]
        timings = result.timings
        t0 = timings.setdefault("received", time.perf_counter())

        def mark(name: str) -> None:
            timings[name] = time.perf_counter()

        async with self._limiter.slot():
            mark("slot")
            await self.start()
            thread_params: Dict[str, Any] = {
                "cwd": str(Path(settings.codex_workdir).expanduser()),
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": settings.sandbox_mode,
                "baseInstructions": instructions or DEFAULT_INSTRUCTIONS,
            }
            if model:
                thread_params["model"] = model
            thread = await self._request("thread/start", thread_params)
            thread_id = thread["thread"]["id"]
            model = result.model = model or thread.get("model")
            effort = self.resolve_effort(model, effort)
            queue: asyncio.Queue = asyncio.Queue()
            self._threads[thread_id] = queue
            mark("thread_started")
            turn_id: Optional[str] = None
            completed = False
            try:
                user_input: List[Dict[str, Any]] = [{"type": "text", "text": text}]
                for image in images or []:
                    if image.startswith(("http://", "https://")):
                        user_input.append({"type": "image", "url": image})
                    else:
                        user_input.append({"type": "localImage", "path": image})
                params: Dict[str, Any] = {"threadId": thread_id, "input": user_input}
                if effort:
                    params["effort"] = effort
                if output_schema is not None:
                    params["outputSchema"] = output_schema
                turn = await self._request("turn/start", params)
                turn_id = (turn.get("turn") or {}).get("id")
                mark("turn_started")

                deadline = t0 + settings.timeout_seconds
                message_texts: Dict[str, str] = {}
                last_item: Optional[str] = None
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=max(0.0, deadline - time.perf_counter()))
                    except asyncio.TimeoutError:
                        raise CodexError("codex execution timed out", status_code=504)
                    method = msg.get("method")
                    p = msg.get("params") or {}
                    if method == "item/agentMessage/delta":
                        delta = p.get("delta") or ""
                        item_id = p.get("itemId")
                        if not delta:
                            continue
                        if "first_token" not in timings:
                            mark("first_token")
                        message_texts[item_id] = message_texts.get(item_id, "") + delta
                        if last_item is not None and item_id != last_item:
                            # A second agent message in the same turn: keep them apart.
                            delta = "\n\n" + delta
                        last_item = item_id
                        yield delta
                    elif method == "item/completed":
                        item = p.get("item") or {}
                        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                            message_texts[item.get("id")] = item["text"]
                            result.text = item["text"]
                    elif method == "thread/tokenUsage/updated":
                        last = (p.get("tokenUsage") or {}).get("last") or {}
                        result.usage = {
                            "input_tokens": int(last.get("inputTokens") or 0),
                            "cached_input_tokens": int(last.get("cachedInputTokens") or 0),
                            "output_tokens": int(last.get("outputTokens") or 0),
                            "reasoning_output_tokens": int(last.get("reasoningOutputTokens") or 0),
                            "total_tokens": int(last.get("totalTokens") or 0),
                        }
                    elif method == "error":
                        logger.warning("[%s] app-server error notification: %s", request_id, json.dumps(p)[:500])
                    elif method == "turn/completed":
                        completed = True
                        turn_obj = p.get("turn") or {}
                        if turn_obj.get("status") != "completed":
                            error = turn_obj.get("error") or {}
                            message = error.get("message") or f"turn {turn_obj.get('status')}"
                            raise CodexError(message, status_code=_turn_error_status(error))
                        break
                    elif method == "_closed":
                        completed = True
                        raise CodexError(p.get("message") or "codex app-server stopped")
                if not result.text:
                    result.text = "\n\n".join(t for t in message_texts.values() if t)
                mark("done")
            finally:
                self._threads.pop(thread_id, None)
                if self.running:
                    if not completed and turn_id:
                        self._forget("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    self._forget("thread/unsubscribe", {"threadId": thread_id})
                _log_timings(request_id, model, effort, timings, result.usage, completed)


def _log_timings(
    request_id: str,
    model: Optional[str],
    effort: Optional[str],
    timings: Dict[str, float],
    usage: Optional[Dict[str, int]],
    completed: bool,
) -> None:
    start = timings["received"]

    def ms(name: str) -> str:
        value = timings.get(name)
        return f"{(value - start) * 1000:.0f}ms" if value is not None else "-"

    end = timings.get("done") or time.perf_counter()
    logger.info(
        "[%s] codex model=%s effort=%s slot=%s thread=%s turn=%s first_token=%s total=%.0fms%s%s",
        request_id,
        model,
        effort or "default",
        ms("slot"),
        ms("thread_started"),
        ms("turn_started"),
        ms("first_token"),
        (end - start) * 1000,
        f" in={usage['input_tokens']} cached={usage['cached_input_tokens']} out={usage['output_tokens']}" if usage else "",
        "" if completed else " (aborted)",
    )


codex = AppServerClient()

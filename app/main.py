import asyncio
import json
import logging
import logging.config
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from uvicorn.config import LOGGING_CONFIG

from .codex import DEFAULT_INSTRUCTIONS, CodexError, TurnResult, codex, warn_if_global_agents_md
from .deps import rate_limiter, verify_api_key
from .images import resolve_image
from .models import list_model_ids, parse_model
from .prompt import normalize_responses_input, split_messages
from .schemas import (
    ChatChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessageResponse,
    CompletionTokensDetails,
    PromptTokensDetails,
    ResponsesMessage,
    ResponsesObject,
    ResponsesOutputText,
    ResponsesRequest,
    ResponsesUsage,
    Usage,
)

# Timestamped uvicorn logs, and route this package's loggers through the same handler.
# The uvicorn CLI configures logging before importing this module, so re-apply it.
LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(asctime)s [%(name)s] %(levelprefix)s %(message)s"
LOGGING_CONFIG["formatters"]["access"]["fmt"] = (
    '%(asctime)s [%(name)s] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
)
LOGGING_CONFIG.setdefault("loggers", {})["app"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
logging.config.dictConfig(LOGGING_CONFIG)

logger = logging.getLogger("app.main")

JSON_MODE_HINT = "Respond with a single valid JSON object and nothing else (no prose, no code fences)."


async def _warm_codex() -> None:
    try:
        await codex.start()
    except Exception as exc:  # logged; retried on the first request
        logger.warning("codex app-server failed to start (will retry on first request): %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    warn_if_global_agents_md()
    warm = asyncio.create_task(_warm_codex())
    try:
        yield
    finally:
        if not warm.done():
            warm.cancel()
        await codex.close()


app = FastAPI(title="Codex Wrapper", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_guards = [Depends(rate_limiter), Depends(verify_api_key)]


# --- helpers -------------------------------------------------------------------


@dataclass
class _Job:
    """One generation request, independent of the API shape it came from."""

    instructions: Optional[str]
    text: str
    model: Optional[str]
    effort: Optional[str] = None
    output_schema: Optional[Dict[str, Any]] = None
    images: List[str] = field(default_factory=list)
    temp_files: List[str] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def stream(self, result: TurnResult) -> AsyncIterator[str]:
        return codex.run_turn(
            text=self.text,
            model=self.model,
            instructions=self.instructions,
            effort=self.effort,
            output_schema=self.output_schema,
            images=self.images,
            result=result,
            request_id=self.request_id,
        )

    def cleanup(self) -> None:
        for path in self.temp_files:
            try:
                os.remove(path)
            except OSError:
                pass


async def _ensure_models() -> None:
    if not codex.models:
        try:
            await codex.start()
        except CodexError as e:
            raise _http_error(e)


async def _build_job(
    messages: List[Dict[str, Any]],
    requested_model: Optional[str],
    requested_effort: Optional[str],
    response_format: Optional[Dict[str, Any]] = None,
) -> _Job:
    await _ensure_models()
    try:
        model, suffix_effort = parse_model(requested_model, codex.models)
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail={"message": str(e), "type": "invalid_request_error", "code": "model_not_found"},
        )
    output_schema, json_mode = _parse_response_format(response_format)
    instructions, text, image_urls = split_messages(messages)
    if json_mode:
        instructions = f"{instructions or DEFAULT_INSTRUCTIONS}\n\n{JSON_MODE_HINT}"
    job = _Job(
        instructions=instructions,
        text=text,
        model=model,
        effort=requested_effort or suffix_effort,
        output_schema=output_schema,
    )
    try:
        for url in image_urls:
            resolved = resolve_image(url)
            job.images.append(resolved)
            if resolved != url:
                job.temp_files.append(resolved)
    except ValueError as e:
        job.cleanup()
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(
        "[%s] request model=%s effort=%s format=%s images=%d",
        job.request_id, model or "default", job.effort or "default",
        "json_schema" if output_schema else ("json_object" if json_mode else "text"), len(job.images),
    )
    return job


def _parse_response_format(response_format: Optional[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return (output JSON schema, json_object mode) for an OpenAI response_format."""

    if not response_format:
        return None, False
    kind = response_format.get("type")
    if kind in (None, "text"):
        return None, False
    if kind == "json_object":
        return None, True
    if kind == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            raise HTTPException(status_code=400, detail="response_format.json_schema.schema must be an object")
        return schema, False
    raise HTTPException(status_code=400, detail=f"Unsupported response_format type '{kind}'")


def _http_error(e: CodexError) -> HTTPException:
    return HTTPException(
        status_code=e.status_code,
        detail={"message": str(e), "type": "server_error" if e.status_code >= 500 else "upstream_error", "code": None},
    )


async def _complete(job: _Job, result: TurnResult) -> str:
    try:
        parts = [delta async for delta in job.stream(result)]
    except CodexError as e:
        raise _http_error(e)
    finally:
        job.cleanup()
    return result.text or "".join(parts)


async def _open_stream(job: _Job, result: TurnResult) -> Tuple[AsyncIterator[str], Optional[str]]:
    """Start a stream and wait for its first delta, so startup errors become HTTP errors."""

    stream = job.stream(result)
    try:
        return stream, await stream.__anext__()
    except StopAsyncIteration:
        return stream, None
    except CodexError as e:
        job.cleanup()
        raise _http_error(e)
    except BaseException:
        job.cleanup()
        raise


def _sse(payload: Dict[str, Any], event: Optional[str] = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# --- endpoints -----------------------------------------------------------------


@app.get("/v1/models", dependencies=_guards)
async def list_models():
    await _ensure_models()
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 0, "owned_by": "codex"} for m in list_model_ids(codex.models)],
    }


def _chat_usage(usage: Optional[Dict[str, int]]) -> Optional[Usage]:
    if not usage:
        return None
    return Usage(
        prompt_tokens=usage["input_tokens"],
        completion_tokens=usage["output_tokens"],
        total_tokens=usage["total_tokens"] or usage["input_tokens"] + usage["output_tokens"],
        prompt_tokens_details=PromptTokensDetails(cached_tokens=usage["cached_input_tokens"]),
        completion_tokens_details=CompletionTokensDetails(reasoning_tokens=usage["reasoning_output_tokens"]),
    )


@app.post("/v1/chat/completions", dependencies=_guards)
async def chat_completions(req: ChatCompletionRequest):
    result = TurnResult(timings={"received": time.perf_counter()})
    job = await _build_job(
        [m.model_dump() for m in req.messages], req.model, req.reasoning_effort, req.response_format
    )
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if not req.stream:
        text = await _complete(job, result)
        resp = ChatCompletionResponse(
            id=completion_id,
            created=created,
            model=req.model or result.model or "codex",
            choices=[ChatChoice(message=ChatMessageResponse(content=text))],
            usage=_chat_usage(result.usage),
        )
        return JSONResponse(resp.model_dump(exclude_none=True))

    stream, first = await _open_stream(job, result)
    model_name = req.model or result.model or "codex"
    include_usage = bool((req.stream_options or {}).get("include_usage"))

    def chunk(choices: List[Dict[str, Any]], **extra: Any) -> bytes:
        return _sse({
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": choices,
            **extra,
        })

    def delta(text: str) -> bytes:
        return chunk([{"index": 0, "delta": {"content": text}, "finish_reason": None}])

    async def events() -> AsyncIterator[bytes]:
        try:
            yield chunk([{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}])
            if first:
                yield delta(first)
            async for text in stream:
                yield delta(text)
            yield chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}])
            usage = _chat_usage(result.usage)
            if include_usage and usage:
                yield chunk([], usage=usage.model_dump())
        except CodexError as e:
            logger.warning("[%s] stream failed: %s", job.request_id, e)
            yield _sse({"error": {"message": str(e), "type": "server_error", "code": e.status_code}})
        finally:
            await stream.aclose()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers=_SSE_HEADERS, background=BackgroundTask(job.cleanup)
    )


@app.post("/v1/responses", dependencies=_guards)
async def responses_endpoint(req: ResponsesRequest):
    result = TurnResult(timings={"received": time.perf_counter()})
    try:
        messages = normalize_responses_input(req.input, req.instructions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job = await _build_job(messages, req.model, req.reasoning.effort if req.reasoning else None)
    resp_id = f"resp_{uuid.uuid4().hex}"
    msg_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())

    def final_object(text: str) -> Dict[str, Any]:
        u = result.usage
        return ResponsesObject(
            id=resp_id,
            created=created,
            model=req.model or result.model or "codex",
            output=[ResponsesMessage(id=msg_id, content=[ResponsesOutputText(text=text)])],
            usage=ResponsesUsage(
                input_tokens=u["input_tokens"],
                output_tokens=u["output_tokens"],
                total_tokens=u["total_tokens"] or u["input_tokens"] + u["output_tokens"],
            ) if u else None,
        ).model_dump(exclude_none=True)

    if not req.stream:
        return JSONResponse(final_object(await _complete(job, result)))

    stream, first = await _open_stream(job, result)
    model_name = req.model or result.model or "codex"

    async def events() -> AsyncIterator[bytes]:
        parts: List[str] = []
        try:
            yield _sse({"id": resp_id, "object": "response", "created": created, "model": model_name,
                        "status": "in_progress"}, "response.created")
            if first:
                parts.append(first)
                yield _sse({"id": resp_id, "delta": first}, "response.output_text.delta")
            async for text in stream:
                parts.append(text)
                yield _sse({"id": resp_id, "delta": text}, "response.output_text.delta")
            final_text = result.text or "".join(parts)
            yield _sse({"id": resp_id, "text": final_text}, "response.output_text.done")
            yield _sse(final_object(final_text), "response.completed")
        except CodexError as e:
            yield _sse({"id": resp_id, "error": {"message": str(e)}}, "response.error")
        finally:
            await stream.aclose()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers=_SSE_HEADERS, background=BackgroundTask(job.cleanup)
    )

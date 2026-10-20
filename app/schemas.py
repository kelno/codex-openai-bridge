from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    # A string or a list of OpenAI content parts ({type: "text" | "image_url", ...}).
    content: Any = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: bool = False
    stream_options: Optional[Dict[str, Any]] = None
    reasoning_effort: Optional[str] = None
    # {"type": "text" | "json_object" | "json_schema", "json_schema": {name, schema, strict}}
    response_format: Optional[Dict[str, Any]] = None
    # Accepted for compatibility with OpenAI clients; Codex does not expose them.
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ChatMessageResponse(BaseModel):
    role: str = "assistant"
    content: str


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessageResponse
    finish_reason: str = "stop"


class PromptTokensDetails(BaseModel):
    cached_tokens: int = 0


class CompletionTokensDetails(BaseModel):
    reasoning_tokens: int = 0


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: PromptTokensDetails
    completion_tokens_details: CompletionTokensDetails


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    # Omitted from the JSON when the backend did not report usage.
    usage: Optional[Usage] = None


# --- Responses API (minimal subset) ---

class ResponsesReasoning(BaseModel):
    effort: Optional[str] = None


class ResponsesRequest(BaseModel):
    model: Optional[str] = None
    input: Any
    instructions: Optional[str] = None
    stream: bool = False
    reasoning: Optional[ResponsesReasoning] = None


class ResponsesOutputText(BaseModel):
    type: Literal["output_text"] = "output_text"
    text: str


class ResponsesMessage(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: str = "assistant"
    content: List[ResponsesOutputText]


class ResponsesUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ResponsesObject(BaseModel):
    id: str
    object: Literal["response"] = "response"
    created: int
    model: str
    status: Literal["in_progress", "completed", "failed"] = "completed"
    output: List[ResponsesMessage] = Field(default_factory=list)
    usage: Optional[ResponsesUsage] = None

from typing import Any, Dict, List, Optional, Tuple

_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "tool": "Tool", "function": "Tool"}


def _content_to_text(content: Any) -> str:
    """Best-effort conversion of message `content` into plain text.

    Accepts a string, a list of OpenAI content parts (`text`/`input_text`), or a
    list of strings. Non-text parts (images, ...) are skipped.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("text", "input_text") and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return str(content)


def _extract_images(content: Any) -> List[str]:
    """Collect image URLs from OpenAI content parts."""
    images: List[str] = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("image_url", "input_image", "image"):
                url_obj = p.get("image_url") or p.get("url")
                url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
                if isinstance(url, str):
                    images.append(url)
    return images


def split_messages(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], str, List[str]]:
    """Split chat messages into (instructions, turn input text, image URLs).

    System and developer messages become the thread's base instructions. A
    single user message is passed through verbatim; a longer conversation is
    rendered as a role-labelled transcript ending with the latest message.
    """
    system_parts: List[str] = []
    convo: List[Tuple[str, str]] = []
    images: List[str] = []

    for m in messages:
        role = (m.get("role") or "").strip().lower() or "user"
        content = m.get("content")
        images.extend(_extract_images(content))
        text = _content_to_text(content)
        if role in ("system", "developer"):
            if text.strip():
                system_parts.append(text.strip())
        else:
            convo.append((role, text))

    instructions = "\n\n".join(system_parts) or None
    if len(convo) == 1 and convo[0][0] == "user":
        return instructions, convo[0][1], images

    lines = [f"{_ROLE_LABELS.get(role, role.capitalize())}: {text.strip()}" for role, text in convo]
    return instructions, "\n\n".join(lines), images


def normalize_responses_input(inp: Any, instructions: Optional[str] = None) -> List[Dict[str, Any]]:
    """Normalize a Responses API `input` (plus optional `instructions`) into chat messages."""

    messages: List[Dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(inp, str):
        return messages + [{"role": "user", "content": inp}]
    if isinstance(inp, list):
        if inp and isinstance(inp[0], dict) and "type" in inp[0] and "role" not in inp[0]:
            return messages + [{"role": "user", "content": inp}]
        if all(isinstance(x, dict) and "role" in x and "content" in x for x in inp):
            return messages + [{"role": str(x["role"]), "content": x["content"]} for x in inp]
        if all(isinstance(x, str) for x in inp):
            return messages + [{"role": "user", "content": "".join(inp)}]
    raise ValueError("Unsupported input format for Responses API")

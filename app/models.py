"""Model names exposed by the API, taken from the app-server's model catalog."""

from typing import Dict, List, Optional, Tuple

from .codex import ModelInfo

# Effort names a client may append to any model id ("gpt-5.6-luna low"), in
# addition to the efforts each model reports.
EFFORT_SUFFIXES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def list_model_ids(models: Dict[str, ModelInfo]) -> List[str]:
    """Visible models followed by their "<model> <effort>" aliases."""

    visible = [m for m in models.values() if not m.hidden]
    ids = [m.id for m in visible]
    for m in visible:
        ids.extend(f"{m.id} {effort}" for effort in m.efforts)
    return ids


def parse_model(requested: Optional[str], models: Dict[str, ModelInfo]) -> Tuple[Optional[str], Optional[str]]:
    """Return (model id, effort from the name suffix) or raise ValueError.

    An empty name means "the model configured in Codex's config.toml" (None).
    """

    name = " ".join((requested or "").split())
    if not name:
        return None, None
    if name in models:
        return name, None
    base, _, suffix = name.rpartition(" ")
    if base in models and (suffix.lower() in EFFORT_SUFFIXES or suffix.lower() in models[base].efforts):
        return base, suffix.lower()
    available = ", ".join(list_model_ids(models)) or "none (codex app-server not ready)"
    raise ValueError(f"Model '{requested}' is not available. Choose one of: {available}")

import base64
import binascii
import mimetypes
import tempfile

from .config import settings


def resolve_image(url: str) -> str:
    """Turn an image URL from a request into something Codex can read.

    ``data:`` URIs are written to a temporary file in the work directory and the
    file path is returned (the caller deletes it). http(s) URLs are passed
    through for Codex to fetch. Anything else (``file://``, bare paths) is
    rejected so clients cannot make the server read local files.
    """

    if url.startswith(("http://", "https://")):
        return url
    if not url.startswith("data:") or "," not in url:
        raise ValueError("image_url must be an http(s) URL or a data: URI")
    header, payload = url.split(",", 1)
    mime = header[5:].split(";")[0] or "image/png"
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 image data: {exc}") from exc
    suffix = mimetypes.guess_extension(mime) or ".png"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=settings.codex_workdir) as fh:
        fh.write(data)
        return fh.name

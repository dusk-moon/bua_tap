import json
import os
from pathlib import Path


def redact_text(text: str) -> str:
    for name in ("XAI_API_KEY", "OPENAI_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def redact_json(value, *, indent=None):
    return redact_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=indent))


def write_artifact(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(redact_json(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

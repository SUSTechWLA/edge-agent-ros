"""Canonical event bytes and public trace redaction shared by producers and consumers."""
import json
import re


SECRET = r"(?:password|passwd|secret|access[_-]?token|refresh[_-]?token|api[_-]?key|authorization|credential|cookie|private[_-]?key|token)"


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",",":")).encode()


def clean_text(value):
    value = re.sub(r"(?i)\bBearer\s+[\w./+=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{12,})", "[REDACTED]", value)
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
    return re.sub(r"""(?i)(?<![\w-])(["']?[\w-]{0,64}""" + SECRET +
                  r"""["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;&}]+)""",
                  r'\1"[REDACTED]"', value)


def sanitized(value):
    if isinstance(value,dict):
        return {k:("[REDACTED]" if re.search(SECRET,k,re.I) else sanitized(v)) for k,v in value.items()}
    if isinstance(value,list):
        return [sanitized(v) for v in value]
    if isinstance(value,str):
        # Ollama's user message and response may themselves contain serialized JSON.
        if value.lstrip().startswith(("{","[")):
            try:
                nested=json.loads(value)
                return json.dumps(sanitized(nested),ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        return clean_text(value)
    return value

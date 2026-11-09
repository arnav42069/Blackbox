import os
import re
import secrets
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        with temporary.open("xb") as f:
            os.chmod(temporary, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clean_text(value: str) -> str:
    value = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", value)
    value = "".join(c for c in value if c in "\n\t" or 32 <= ord(c) != 127)
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)", "[REDACTED PRIVATE KEY]", value, flags=re.S)
    value = re.sub(r"(?i)([\"']?(?:password|passwd|api[_-]?key|access[_-]?token|secret|authorization)[\"']?\s*[:=]\s*)([\"'])([^\n]*?)\2", lambda m: m[1] + m[2] + '[REDACTED]' + m[2], value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", 'Bearer [REDACTED]', value)
    value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+|basic\s+)?|(?:password|passwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    return value


def clean_value(value):
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_value(v) for v in value]
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.fullmatch(r'(?i)(password|passwd|api[_-]?key|access[_-]?token|secret|authorization)', k) else clean_value(v) for k, v in value.items()}
    return value

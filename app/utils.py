from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_slug(value: str, fallback: str = "project") -> str:
    value = value.strip()
    if not value:
        return fallback
    value = _SLUG_RE.sub("-", value).strip("-").lower()
    return value or fallback


def json_dump(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except Exception:
            return None
    except Exception:
        return None


def file_category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".c", ".h"}:
        return "c"
    if suffix in {".dts", ".dtsi"}:
        return "dts"
    if suffix in {".md", ".rst", ".txt"}:
        return "doc"
    if suffix in {".json", ".yaml", ".yml"}:
        return "config"
    if suffix in {".S", ".s"}:
        return "asm"
    return "other"


def iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return (p for p in root.rglob("*") if p.is_file())


def sanitize_artifact_name(name: str) -> str:
    if not _ARTIFACT_NAME_RE.match(name):
        raise ValueError(f"Invalid artifact name: {name}")
    return name


def strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            body = "\n".join(lines[1:-1])
            return body.strip() + "\n"
    return text


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 16] + "\n/* ...truncated... */\n"


def tokenize_keywords(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())


from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .utils import ensure_dir, file_category, iter_files, json_dump, read_text_safe, tokenize_keywords


FUNC_HEADER_RE = re.compile(
    r"(?m)^[ \t]*(?:static\s+|inline\s+|extern\s+|const\s+|volatile\s+|__\w+\s+)*"
    r"[A-Za-z_][A-Za-z0-9_\s\*\(\)]*?\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*\{"
)
MACRO_START_RE = re.compile(r"(?m)^[ \t]*#define\s+([A-Za-z_][A-Za-z0-9_]*)\b")


def _line_number_from_pos(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _extract_brace_block(text: str, open_brace_pos: int) -> tuple[int, int] | None:
    depth = 0
    for idx in range(open_brace_pos, len(text)):
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_brace_pos, idx + 1
    return None


def _chunk_c_like(path: str, bucket: str, content: str, chunk_id_start: int) -> tuple[list[dict[str, Any]], int]:
    chunks: list[dict[str, Any]] = []
    cid = chunk_id_start
    lines = content.splitlines()
    chunks.append(
        {
            "id": f"chunk-{cid}",
            "chunk_type": "file",
            "name": Path(path).name,
            "path": path,
            "bucket": bucket,
            "start_line": 1,
            "end_line": max(1, len(lines)),
            "text": content,
        }
    )
    cid += 1

    for m in MACRO_START_RE.finditer(content):
        start = m.start()
        start_line = _line_number_from_pos(content, start)
        macro_name = m.group(1)
        macro_lines = []
        source_lines = content.splitlines()
        i = start_line - 1
        while i < len(source_lines):
            macro_lines.append(source_lines[i])
            if not source_lines[i].rstrip().endswith("\\"):
                break
            i += 1
        end_line = start_line + len(macro_lines) - 1
        macro_text = "\n".join(macro_lines).strip() + "\n"
        chunks.append(
            {
                "id": f"chunk-{cid}",
                "chunk_type": "macro",
                "name": macro_name,
                "path": path,
                "bucket": bucket,
                "start_line": start_line,
                "end_line": end_line,
                "text": macro_text,
            }
        )
        cid += 1

    for m in FUNC_HEADER_RE.finditer(content):
        func_name = m.group(1)
        open_brace_pos = content.find("{", m.start(), m.end())
        if open_brace_pos < 0:
            continue
        block = _extract_brace_block(content, open_brace_pos)
        if not block:
            continue
        _, block_end = block
        header_start = m.start()
        start_line = _line_number_from_pos(content, header_start)
        end_line = _line_number_from_pos(content, block_end)
        func_text = content[header_start:block_end].strip() + "\n"
        chunks.append(
            {
                "id": f"chunk-{cid}",
                "chunk_type": "function",
                "name": func_name,
                "path": path,
                "bucket": bucket,
                "start_line": start_line,
                "end_line": end_line,
                "text": func_text,
            }
        )
        cid += 1

    return chunks, cid


def _chunk_generic(path: str, bucket: str, content: str, chunk_id_start: int) -> tuple[list[dict[str, Any]], int]:
    cid = chunk_id_start
    lines = content.splitlines()
    chunks: list[dict[str, Any]] = []
    chunks.append(
        {
            "id": f"chunk-{cid}",
            "chunk_type": "file",
            "name": Path(path).name,
            "path": path,
            "bucket": bucket,
            "start_line": 1,
            "end_line": max(1, len(lines)),
            "text": content,
        }
    )
    cid += 1
    if len(lines) > 200:
        for offset in range(0, len(lines), 200):
            part = lines[offset : offset + 200]
            chunks.append(
                {
                    "id": f"chunk-{cid}",
                    "chunk_type": "segment",
                    "name": f"{Path(path).name}:{offset+1}",
                    "path": path,
                    "bucket": bucket,
                    "start_line": offset + 1,
                    "end_line": min(len(lines), offset + len(part)),
                    "text": "\n".join(part) + "\n",
                }
            )
            cid += 1
    return chunks, cid


def _build_index(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    keyword_to_chunks: dict[str, list[str]] = defaultdict(list)
    register_names: set[str] = set()
    function_names: set[str] = set()
    macro_names: set[str] = set()

    for chunk in chunks:
        for token in tokenize_keywords(chunk["text"]):
            if len(keyword_to_chunks[token]) < 20:
                keyword_to_chunks[token].append(chunk["id"])
        for reg in re.findall(r"\b(?:REG|OFFSET|STAT|STATUS|CTRL|CONTROL|INT|IRQ|DMA|FIFO)_[A-Za-z0-9_]+\b", chunk["text"]):
            register_names.add(reg)
        if chunk["chunk_type"] == "function":
            function_names.add(chunk["name"])
        if chunk["chunk_type"] == "macro":
            macro_names.add(chunk["name"])

    return {
        "keywords": dict(sorted(keyword_to_chunks.items())),
        "register_names": sorted(register_names),
        "function_names": sorted(function_names),
        "macro_names": sorted(macro_names),
        "chunk_count": len(chunks),
    }


def _scan_bucket(
    root: Path, bucket: str, chunk_id_start: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    file_records: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    cid = chunk_id_start
    for file_path in sorted(iter_files(root), key=lambda p: str(p)):
        rel = file_path.relative_to(root).as_posix()
        category = file_category(file_path)
        record = {
            "path": f"{bucket}/{rel}",
            "bucket": bucket,
            "category": category,
            "size_bytes": file_path.stat().st_size,
        }
        file_records.append(record)
        text = read_text_safe(file_path)
        if text is None:
            continue
        if file_path.suffix.lower() in {".c", ".h", ".dts", ".dtsi"}:
            new_chunks, cid = _chunk_c_like(record["path"], bucket, text, cid)
        else:
            new_chunks, cid = _chunk_generic(record["path"], bucket, text, cid)
        chunks.extend(new_chunks)
    return file_records, chunks, cid


def run_ingest(project_root: Path) -> dict[str, Any]:
    uploads_dir = project_root / "uploads"
    artifacts_dir = ensure_dir(project_root / "artifacts")
    driver_root = uploads_dir / "driver"
    ref_root = uploads_dir / "reference"

    next_cid = 1
    if driver_root.exists():
        driver_files, driver_chunks, next_cid = _scan_bucket(driver_root, "driver", next_cid)
    else:
        driver_files, driver_chunks = [], []
    if ref_root.exists():
        ref_files, ref_chunks, next_cid = _scan_bucket(ref_root, "reference", next_cid)
    else:
        ref_files, ref_chunks = [], []

    chunks = driver_chunks + ref_chunks
    index = _build_index(chunks)
    ingest = {
        "driver_root_exists": driver_root.exists(),
        "reference_root_exists": ref_root.exists(),
        "file_count": len(driver_files) + len(ref_files),
        "files": driver_files + ref_files,
        "chunks_count": len(chunks),
    }

    json_dump(artifacts_dir / "ingest.json", ingest)
    json_dump(artifacts_dir / "chunks.json", {"chunks": chunks})
    json_dump(artifacts_dir / "index.json", index)
    return {"ingest": ingest, "chunks": chunks, "index": index}

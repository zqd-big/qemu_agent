from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .utils import json_dump, json_load, read_text_safe, utc_now_iso


DEFINE_RE = re.compile(r"(?m)^\s*#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+\(?((?:0x[0-9A-Fa-f]+|\d+))\)?")
READ_RE = re.compile(r"\b(read[blw])\s*\(([^)]*)\)")
WRITE_RE = re.compile(r"\b(write[blw])\s*\((.*?),(.*?)\)")
STATE_STRUCT_RE = re.compile(r"typedef\s+struct\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", re.M)
MEMORY_OPS_RE = re.compile(r"\bMemoryRegionOps\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{")
VMSTATE_RE = re.compile(r"\bVMStateDescription\s+([A-Za-z_][A-Za-z0-9_]*)\b")


def _line_snippet(lines: list[str], idx: int) -> str:
    return lines[idx].strip()[:300]


def _extract_offset_expr(expr: str) -> str | None:
    m = re.search(r"\+\s*([A-Za-z_][A-Za-z0-9_]*|0x[0-9A-Fa-f]+|\d+)", expr)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z][A-Z0-9_]{2,})\b", expr)
    if m:
        return m.group(1)
    return None


def _analyse_driver_file(rel_path: str, text: str) -> dict[str, Any]:
    lines = text.splitlines()
    reg_defines = []
    mmio_accesses = []
    polling = []
    irq_clues = []
    dma_clues = []
    reset_clues = []

    for m in DEFINE_RE.finditer(text):
        name, value = m.groups()
        line = text.count("\n", 0, m.start()) + 1
        if any(k in name.upper() for k in ("REG", "OFFSET", "STAT", "CTRL", "INT", "IRQ", "FIFO", "DMA")):
            reg_defines.append({"name": name, "value": value, "file": rel_path, "line": line})

    for idx, line in enumerate(lines):
        line_no = idx + 1
        for m in READ_RE.finditer(line):
            op, arg = m.groups()
            mmio_accesses.append(
                {
                    "op": op,
                    "addr_expr": arg.strip(),
                    "offset_expr": _extract_offset_expr(arg),
                    "file": rel_path,
                    "line": line_no,
                    "evidence": _line_snippet(lines, idx),
                }
            )
        for m in WRITE_RE.finditer(line):
            op, val, addr = m.groups()
            mmio_accesses.append(
                {
                    "op": op,
                    "value_expr": val.strip(),
                    "addr_expr": addr.strip(),
                    "offset_expr": _extract_offset_expr(addr),
                    "file": rel_path,
                    "line": line_no,
                    "evidence": _line_snippet(lines, idx),
                }
            )

        lower = line.lower()
        if "read_poll_timeout" in lower or (("while" in lower or "for" in lower) and "readl" in lower):
            polling.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})
        if any(k in lower for k in ("udelay", "mdelay", "msleep", "usleep", "timeout")) and (
            "poll" in lower or "read" in lower or "wait" in lower
        ):
            polling.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})
        if any(k in lower for k in ("irq", "interrupt", "int_status", "int_mask", "int_en")):
            irq_clues.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})
        if any(k in lower for k in ("dma", "desc", "descriptor", "sg")):
            dma_clues.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})
        if any(k in lower for k in ("reset", "probe", "init", "default")):
            reset_clues.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})

    return {
        "register_defines": reg_defines[:300],
        "mmio_accesses": mmio_accesses[:800],
        "polling_patterns": polling[:200],
        "irq_clues": irq_clues[:200],
        "dma_clues": dma_clues[:200],
        "reset_default_clues": reset_clues[:200],
    }


def _analyse_reference_file(rel_path: str, text: str) -> dict[str, Any]:
    lines = text.splitlines()
    state_structs = []
    memory_ops = []
    vmstates = []
    irq_logic = []
    fifo_logic = []
    read_write_callbacks = []

    for m in STATE_STRUCT_RE.finditer(text):
        name = m.group(1)
        line = text.count("\n", 0, m.start()) + 1
        if name.endswith("State") or "state" in name.lower():
            state_structs.append({"name": name, "file": rel_path, "line": line})

    for m in MEMORY_OPS_RE.finditer(text):
        memory_ops.append({"name": m.group(1), "file": rel_path, "line": text.count("\n", 0, m.start()) + 1})

    for m in VMSTATE_RE.finditer(text):
        vmstates.append({"name": m.group(1), "file": rel_path, "line": text.count("\n", 0, m.start()) + 1})

    for idx, line in enumerate(lines):
        lower = line.lower()
        line_no = idx + 1
        if "qemu_set_irq" in line or (".irq" in line and ("update" in lower or "set" in lower)):
            irq_logic.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})
        if "fifo" in lower or "ring" in lower:
            fifo_logic.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})
        if re.search(r"\bstatic\b.*\b(read|write)\b.*\(", line):
            read_write_callbacks.append({"file": rel_path, "line": line_no, "evidence": _line_snippet(lines, idx)})

    return {
        "state_structs": state_structs[:100],
        "memory_region_ops": memory_ops[:100],
        "vmstate_descriptions": vmstates[:100],
        "irq_logic_clues": irq_logic[:200],
        "fifo_logic_clues": fifo_logic[:200],
        "read_write_callbacks": read_write_callbacks[:200],
    }


def _collect_text_files(project_root: Path, ingest: dict[str, Any], bucket: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for rec in ingest.get("files", []):
        if rec.get("bucket") != bucket:
            continue
        rel = rec["path"]
        local_rel = "/".join(rel.split("/")[1:])
        local_path = project_root / "uploads" / bucket / local_rel
        text = read_text_safe(local_path)
        if text is None:
            continue
        result.append((rel, text))
    return result


def _derive_summary(driver: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    reg_names = {r["name"] for r in driver.get("register_defines", [])}
    for acc in driver.get("mmio_accesses", []):
        off = acc.get("offset_expr")
        if off and re.match(r"[A-Z][A-Z0-9_]+$", off):
            reg_names.add(off)

    irq_regs = [n for n in sorted(reg_names) if any(k in n for k in ("IRQ", "INT", "INTR"))]
    status_regs = [n for n in sorted(reg_names) if any(k in n for k in ("STAT", "STATUS", "SR", "BUSY"))]
    control_regs = [n for n in sorted(reg_names) if any(k in n for k in ("CTRL", "CONTROL", "CR", "CMD"))]
    fifo_regs = [n for n in sorted(reg_names) if "FIFO" in n]
    dma_regs = [n for n in sorted(reg_names) if "DMA" in n or "DESC" in n]

    return {
        "candidate_registers": sorted(reg_names)[:400],
        "likely_irq_regs": irq_regs[:50],
        "likely_status_regs": status_regs[:50],
        "likely_control_regs": control_regs[:50],
        "likely_fifo_regs": fifo_regs[:50],
        "likely_dma_regs": dma_regs[:50],
        "driver_mmio_access_count": len(driver.get("mmio_accesses", [])),
        "driver_polling_pattern_count": len(driver.get("polling_patterns", [])),
        "reference_state_struct_count": len(reference.get("state_structs", [])),
        "reference_memory_ops_count": len(reference.get("memory_region_ops", [])),
    }


def run_analysis(project_root: Path, device_name: str, device_type: str) -> dict[str, Any]:
    artifacts_dir = project_root / "artifacts"
    ingest = json_load(artifacts_dir / "ingest.json")
    chunks_obj = json_load(artifacts_dir / "chunks.json")
    chunks = chunks_obj.get("chunks", [])

    driver = {
        "register_defines": [],
        "mmio_accesses": [],
        "polling_patterns": [],
        "irq_clues": [],
        "dma_clues": [],
        "reset_default_clues": [],
    }
    reference = {
        "state_structs": [],
        "memory_region_ops": [],
        "vmstate_descriptions": [],
        "irq_logic_clues": [],
        "fifo_logic_clues": [],
        "read_write_callbacks": [],
    }

    for rel, text in _collect_text_files(project_root, ingest, "driver"):
        out = _analyse_driver_file(rel, text)
        for k, v in out.items():
            driver[k].extend(v)

    for rel, text in _collect_text_files(project_root, ingest, "reference"):
        out = _analyse_reference_file(rel, text)
        for k, v in out.items():
            reference[k].extend(v)

    derived = _derive_summary(driver, reference)

    analysis = {
        "version": "mvp-1",
        "generated_at": utc_now_iso(),
        "device_name": device_name,
        "device_type": device_type,
        "ingest_summary": {
            "file_count": ingest.get("file_count", 0),
            "chunks_count": ingest.get("chunks_count", 0),
            "driver_files": len([f for f in ingest.get("files", []) if f.get("bucket") == "driver"]),
            "reference_files": len([f for f in ingest.get("files", []) if f.get("bucket") == "reference"]),
            "chunk_type_counts": _count_chunk_types(chunks),
        },
        "driver": driver,
        "reference_qemu": reference,
        "derived": derived,
    }

    json_dump(artifacts_dir / "analysis.json", analysis)
    return analysis


def _count_chunk_types(chunks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        ctype = chunk.get("chunk_type", "unknown")
        counts[ctype] = counts.get(ctype, 0) + 1
    return counts


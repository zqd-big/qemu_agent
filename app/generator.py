from __future__ import annotations

import json
import re
from typing import Any

from .settings import MAX_PROMPT_CHARS
from .utils import strip_markdown_fence, truncate_text, utc_now_iso


def build_generation_messages(
    *,
    device_name: str,
    device_type: str,
    analysis: dict[str, Any],
    answers: dict[str, Any],
    top_chunks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    system_prompt = (
        "You are a QEMU device model engineer. Generate a single complete QEMU device model C source file.\n"
        "Hard constraints:\n"
        "- Output ONLY the C file content.\n"
        "- Do NOT output markdown fences.\n"
        "- Do NOT output explanations.\n"
        "- Do NOT output git patch/diff.\n"
        "- Include QOM type, State struct, MemoryRegionOps read/write callbacks, reset, minimal VMState, and logging.\n"
        "- Add TODO comments to mark inferred/placeholder behavior.\n"
        "- Annotate in comments which access paths are derived from driver evidence where possible.\n"
    )

    prompt_obj: dict[str, Any] = {
        "task": "Generate QEMU device model C source for one device",
        "device_name": device_name,
        "device_type": device_type,
        "required_output_filename": f"{device_name}.c",
        "qemu_minimum_requirements": [
            "TYPE_XXX and type registration",
            "Device state struct with regs/irq/fifo/busy flags as needed",
            "MemoryRegion and read/write callbacks",
            "reset function with defaults",
            "VMStateDescription minimal migration fields",
            "logging via qemu_log_mask or DPRINTF macro",
            "TODO comments for inferred register semantics",
        ],
        "analysis": analysis,
        "answers": answers,
        "retrieved_code_chunks": [],
        "final_output_contract": {
            "format": "single_c_file_only",
            "forbidden": ["markdown", "patch", "explanations", "multiple files"],
        },
    }

    total_chars = len(json.dumps(prompt_obj, ensure_ascii=False))
    for chunk in top_chunks:
        chunk_payload = {
            "id": chunk.get("id"),
            "bucket": chunk.get("bucket"),
            "chunk_type": chunk.get("chunk_type"),
            "path": chunk.get("path"),
            "name": chunk.get("name"),
            "start_line": chunk.get("start_line"),
            "end_line": chunk.get("end_line"),
            "retrieval_score": chunk.get("retrieval_score"),
            "text": truncate_text(chunk.get("text", ""), 5000),
        }
        candidate_chars = len(json.dumps(chunk_payload, ensure_ascii=False))
        if total_chars + candidate_chars > MAX_PROMPT_CHARS and prompt_obj["retrieved_code_chunks"]:
            break
        prompt_obj["retrieved_code_chunks"].append(chunk_payload)
        total_chars += candidate_chars

    user_prompt = json.dumps(prompt_obj, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def sanitize_and_validate_c_output(raw_text: str) -> str:
    text = strip_markdown_fence(raw_text).strip()
    if not text:
        raise ValueError("Model returned empty output")
    if "diff --git" in text or re.search(r"(?m)^@@\s", text) or re.search(r"(?m)^(---|\+\+\+)\s", text):
        raise ValueError("Model returned a patch/diff instead of a C file")
    if "```" in text:
        raise ValueError("Model output contains markdown fences; expected raw C file content")
    if re.search(r"(?im)^\s*(here is|explanation|note:)", text):
        raise ValueError("Model output appears to include prose instead of only C code")

    qemu_markers = [
        "MemoryRegionOps",
        "TypeInfo",
        "VMStateDescription",
        "qemu_log_mask",
        "qemu_set_irq",
        "reset",
        "#include",
    ]
    hit_count = sum(1 for m in qemu_markers if m in text)
    if hit_count < 3:
        raise ValueError("Model output does not look like a QEMU C device model (missing expected markers)")

    if not text.endswith("\n"):
        text += "\n"
    return text


def build_report_markdown(
    *,
    project_id: str,
    device_name: str,
    device_type: str,
    analysis: dict[str, Any],
    answers: dict[str, Any],
    selected_chunks: list[dict[str, Any]],
    c_artifact_name: str,
) -> str:
    derived = analysis.get("derived", {})
    lines = [
        f"# QEMU Device Model Generation Report ({device_name})",
        "",
        f"- Project ID: `{project_id}`",
        f"- Device Type: `{device_type}`",
        f"- Generated At (UTC): `{utc_now_iso()}`",
        f"- C Artifact: `{c_artifact_name}`",
        "",
        "## Inputs Used",
        "",
        f"- Candidate registers detected: {len(derived.get('candidate_registers', []))}",
        f"- Driver MMIO accesses detected: {derived.get('driver_mmio_access_count', 0)}",
        f"- Driver polling patterns detected: {derived.get('driver_polling_pattern_count', 0)}",
        f"- Reference QEMU state structs detected: {derived.get('reference_state_struct_count', 0)}",
        f"- Reference MemoryRegionOps detected: {derived.get('reference_memory_ops_count', 0)}",
        "",
        "## Key Assumptions / TODO Focus",
        "",
        "- W1C/W1S, read-clear, write-trigger, busy timing, FIFO depth, IRQ set/clear are explicitly marked with TODO comments if uncertain.",
        "- Unmodeled registers should keep driver progress moving (preserve writes or return safe defaults) while logging accesses.",
        "- Access paths from driver evidence should be annotated in code comments near relevant callbacks/state transitions.",
        "",
        "## User Answers Snapshot",
        "",
        "```json",
        json.dumps(answers or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Retrieved Context Chunks (Top-K)",
        "",
    ]
    for c in selected_chunks:
        lines.append(
            f"- `{c.get('id')}` {c.get('bucket')}/{c.get('path')} "
            f"({c.get('chunk_type')}:{c.get('start_line')}-{c.get('end_line')}, score={c.get('retrieval_score')})"
        )
    lines.extend(
        [
            "",
            "## Suggested Validation Steps",
            "",
            "1. Enable device logs (`qemu_log_mask`) and confirm driver probe register access sequence matches expected paths.",
            "2. Verify reset defaults allow probe/init to complete without polling timeout.",
            "3. Exercise IRQ status/mask/ack flow and confirm line assertion/deassertion behavior.",
            "4. If FIFO is modeled, test empty/full thresholds and read/write side effects.",
            "5. Add tracepoints/logs around TODO-marked inferred semantics and refine with real hardware traces if available.",
            "",
        ]
    )
    return "\n".join(lines)


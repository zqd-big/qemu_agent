from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import json_dump, utc_now_iso


def _q(qid: str, question: str, why: str, answer_format: str, examples: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": qid,
        "question": question,
        "why": why,
        "answer_format": answer_format,
        "examples": examples or [],
    }


def generate_questions_from_analysis(analysis: dict[str, Any], project_root: Path) -> dict[str, Any]:
    derived = analysis.get("derived", {})
    driver = analysis.get("driver", {})
    device_name = analysis.get("device_name", "device")

    regs = derived.get("candidate_registers", [])
    irq_regs = derived.get("likely_irq_regs", [])
    status_regs = derived.get("likely_status_regs", [])
    ctrl_regs = derived.get("likely_control_regs", [])
    fifo_regs = derived.get("likely_fifo_regs", [])
    dma_regs = derived.get("likely_dma_regs", [])
    top_regs = regs[:12]

    questions: list[dict[str, Any]] = []
    qn = 1

    def add(question: str, why: str, answer_format: str, examples: list[str] | None = None) -> None:
        nonlocal qn
        questions.append(_q(f"q{qn:02d}", question, why, answer_format, examples))
        qn += 1

    add(
        f"For {device_name}, which status/interrupt registers use W1C or W1S semantics? Please list per register/bit.",
        "Incorrect W1C/W1S modeling is a common cause of stuck interrupts and driver init failures.",
        "object",
        [f'{{"{irq_regs[0] if irq_regs else "INT_STATUS"}": {{"semantic": "W1C", "bits": ["DONE", "ERR"]}}}}'],
    )
    add(
        "Which registers/bits have read side effects (read-clear, FIFO pop, latch snapshot, etc.)?",
        "Read side effects directly affect polling loops, data path behavior, and interrupt deassert timing.",
        "object",
        ['{"STATUS": ["DONE(read-clear)"], "RX_FIFO": "read pops one entry"}'],
    )
    add(
        "Which writes trigger actions (START/CMD/DOORBELL/RESET) instead of only storing a value?",
        "MVP code should implement a minimal working state machine from real driver access sequences.",
        "object",
        ['{"CTRL.START=1": "set BUSY, schedule completion", "CMD": "execute immediately"}'],
    )
    add(
        "What is the BUSY/READY/DONE timing model after command start? Include delays/poll-count limits if known.",
        "Drivers often rely on exact status transitions in polling loops and timeout paths.",
        "object",
        ['{"BUSY": "set on START, clear after <=100 polls", "DONE": "set when BUSY clears"}'],
    )
    add(
        "What are IRQ set/clear conditions? Please distinguish raw status, mask/enable, masked status, and line update logic.",
        "QEMU must implement correct qemu_set_irq behavior and status/mask interaction.",
        "object",
        ['{"set": ["RX_READY", "DMA_DONE"], "clear": ["W1C INT_STATUS"], "line_assert": "raw&mask != 0"}'],
    )
    add(
        "What polling timeout behavior does the driver expect (delay function, timeout units, error code)?",
        "This guides the minimum state transition timing needed to avoid spurious timeouts in the guest.",
        "object",
        ['{"delay_us": 10, "timeout_us": 10000, "error": "-ETIMEDOUT"}'],
    )
    add(
        "What is the FIFO model (depth, thresholds, empty/full flags, overrun/underrun behavior)?",
        "FIFO depth and side effects frequently control both data flow and IRQ behavior.",
        "object",
        ['{"tx_depth": 16, "rx_depth": 16, "rx_irq_threshold": 1, "overrun_sets": "ERR"}'],
    )

    if dma_regs or driver.get("dma_clues"):
        add(
            "What is the DMA descriptor format (addr/len/control/next) and completion condition?",
            "DMA support can be implemented as a minimal descriptor walker in the MVP if the format is known.",
            "object",
            ['{"desc": {"addr": "bits[31:2]", "len": "bits[15:0]", "own": "bit31", "next": "word1"}}'],
        )

    add(
        f"What are reset defaults for key registers? Please cover at least: {', '.join(top_regs[:8]) or 'CTRL, STATUS, INT_STATUS'}",
        "Reset defaults determine whether probe/init and first polling loops behave correctly.",
        "object",
        ['{"CTRL": "0x00000000", "STATUS": "0x00000001", "INT_MASK": "0x00000000"}'],
    )
    add(
        "For registers not fully modeled yet, what fallback behavior should be used (return 0 / preserve writes / force ready bits / log only)?",
        "MVP should keep the driver moving while still exposing unknown accesses for debugging.",
        "object",
        ['{"default_read": "0", "preserve_write": ["CFG", "CTRL"], "log_unhandled": true}'],
    )
    add(
        "What access sizes and alignment are valid (8/16/32-bit)? How should invalid accesses behave?",
        "This maps to MemoryRegionOps access constraints and can prevent guest driver regressions.",
        "object",
        ['{"valid_sizes": [4], "unaligned": "log+ignore"}'],
    )

    if status_regs:
        add(
            f"Which status bits are hardware-owned vs software-writable in: {', '.join(status_regs[:6])}?",
            "Status registers are often mixed semantic (RW + HW-updated bits), not plain storage.",
            "object",
            ['{"STATUS": {"hw_only": ["BUSY", "DONE"], "sw_w1c": ["ERR"]}}'],
        )
    if ctrl_regs:
        add(
            f"Which control bits are essential for the minimal state machine in: {', '.join(ctrl_regs[:6])}?",
            "This helps prioritize a working path based on driver evidence instead of modeling everything.",
            "object",
            ['{"CTRL": {"START": 0, "RESET": 1, "IRQ_EN": 4}}'],
        )
    if fifo_regs:
        add(
            f"What are the data direction and side effects of FIFO-related registers: {', '.join(fifo_regs[:6])}?",
            "Needed to decide whether read pops RX FIFO and/or write pushes TX FIFO in the MVP model.",
            "object",
            ['{"RX_FIFO": "read pops", "TX_FIFO": "write pushes"}'],
        )

    deduped: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for q in questions:
        if q["question"] in seen_questions:
            continue
        seen_questions.add(q["question"])
        deduped.append(q)

    result = {
        "version": "mvp-1",
        "generated_at": utc_now_iso(),
        "source": "heuristic-analysis",
        "questions": deduped[:20],
    }

    json_dump(project_root / "artifacts" / "questions.json", result)
    return result


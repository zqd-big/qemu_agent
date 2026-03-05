from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .llm_client import LLMConfigError, LLMRequestError, resolve_llm_target, stream_chat_completion
from .retrieval import select_top_chunks
from .settings import DEFAULT_LLM_CONFIG_PATH
from .utils import json_dump, strip_markdown_fence, utc_now_iso


QUESTION_FORMATS = {"object", "string", "number", "array", "boolean"}


def _q(qid: str, question: str, why: str, answer_format: str, examples: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": qid,
        "question": question,
        "why": why,
        "answer_format": answer_format,
        "examples": examples or [],
    }


def _estimate_question_budget(analysis: dict[str, Any]) -> dict[str, int]:
    derived = analysis.get("derived", {})
    driver = analysis.get("driver", {})
    ref = analysis.get("reference_qemu", {})
    device_type = str(analysis.get("device_type", "")).lower()

    regs = len(derived.get("candidate_registers", []))
    mmio = int(derived.get("driver_mmio_access_count", 0))
    polling = int(derived.get("driver_polling_pattern_count", 0))
    irq = len(derived.get("likely_irq_regs", [])) + len(driver.get("irq_clues", []))
    fifo = len(derived.get("likely_fifo_regs", [])) + len(ref.get("fifo_logic_clues", []))
    dma = len(derived.get("likely_dma_regs", [])) + len(driver.get("dma_clues", []))

    complexity = 0
    if regs >= 8:
        complexity += 1
    if regs >= 20:
        complexity += 1
    if mmio >= 20:
        complexity += 1
    if mmio >= 80:
        complexity += 1
    if polling > 0:
        complexity += 1
    if irq > 0:
        complexity += 1
    if fifo > 0:
        complexity += 1
    if dma > 0:
        complexity += 1
    if device_type in {"eth", "nandc"}:
        complexity += 1

    target = 6 + min(9, complexity)
    target = max(6, min(16, target))
    min_count = max(5, target - 3)
    max_count = min(18, target + 3)
    return {"target": target, "min": min_count, "max": max_count}


def _normalize_question_items(items: list[Any], *, max_items: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        if not question or question in seen_questions:
            continue
        why = str(item.get("why", "")).strip() or "Clarify hardware semantics to reduce wrong QEMU assumptions."
        answer_format = str(item.get("answer_format", "object")).strip().lower()
        if answer_format not in QUESTION_FORMATS:
            answer_format = "object"
        raw_examples = item.get("examples")
        examples: list[str]
        if isinstance(raw_examples, list):
            examples = [str(v) for v in raw_examples if str(v).strip()][:3]
        else:
            examples = []
        raw_refs = item.get("evidence_refs")
        evidence_refs: list[str] = []
        if isinstance(raw_refs, list):
            evidence_refs = [str(v).strip() for v in raw_refs if str(v).strip()][:4]
        elif isinstance(raw_refs, str) and raw_refs.strip():
            evidence_refs = [raw_refs.strip()]
        if not evidence_refs:
            evidence_refs = ["missing-evidence"]
        out.append(
            {
                "question": question,
                "why": why,
                "answer_format": answer_format,
                "examples": examples,
                "evidence_refs": evidence_refs,
            }
        )
        seen_questions.add(question)
        if len(out) >= max_items:
            break
    for idx, item in enumerate(out, start=1):
        item["id"] = f"q{idx:02d}"
    return out


def _extract_json_payload(text: str) -> Any:
    cleaned = strip_markdown_fence(text).strip()
    if not cleaned:
        raise ValueError("LLM returned empty questions payload")

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # extract biggest JSON object/array if wrapped by prose
    obj_match = re.search(r"\{[\s\S]*\}", cleaned)
    arr_match = re.search(r"\[[\s\S]*\]", cleaned)
    candidates = [m.group(0) for m in (obj_match, arr_match) if m]
    if not candidates:
        raise ValueError("LLM questions output is not valid JSON")

    candidates.sort(key=len, reverse=True)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("Failed to parse JSON from LLM questions output")


def _build_question_prompt(
    analysis: dict[str, Any],
    top_chunks: list[dict[str, Any]],
    budget: dict[str, int],
    *,
    compact: bool = False,
    use_no_think: bool = False,
) -> list[dict[str, str]]:
    driver = analysis.get("driver", {})
    ref = analysis.get("reference_qemu", {})
    derived = analysis.get("derived", {})

    reg_cap = 36 if compact else 80
    mmio_cap = 60 if compact else 120
    clue_cap = 24 if compact else 50
    ref_cap = 18 if compact else 30
    callback_cap = 24 if compact else 40
    chunk_text_cap = 1200 if compact else 2500

    driver_evidence = {
        "register_defines": driver.get("register_defines", [])[:reg_cap],
        "mmio_accesses": driver.get("mmio_accesses", [])[:mmio_cap],
        "polling_patterns": driver.get("polling_patterns", [])[:clue_cap],
        "irq_clues": driver.get("irq_clues", [])[:clue_cap],
        "dma_clues": driver.get("dma_clues", [])[:clue_cap],
        "reset_default_clues": driver.get("reset_default_clues", [])[:clue_cap],
    }
    ref_evidence = {
        "state_structs": ref.get("state_structs", [])[:ref_cap],
        "memory_region_ops": ref.get("memory_region_ops", [])[:ref_cap],
        "vmstate_descriptions": ref.get("vmstate_descriptions", [])[:ref_cap],
        "irq_logic_clues": ref.get("irq_logic_clues", [])[:callback_cap],
        "fifo_logic_clues": ref.get("fifo_logic_clues", [])[:callback_cap],
        "read_write_callbacks": ref.get("read_write_callbacks", [])[:callback_cap],
    }
    chunk_payload = []
    for chunk in top_chunks:
        chunk_payload.append(
            {
                "id": chunk.get("id"),
                "path": chunk.get("path"),
                "bucket": chunk.get("bucket"),
                "chunk_type": chunk.get("chunk_type"),
                "start_line": chunk.get("start_line"),
                "end_line": chunk.get("end_line"),
                "score": chunk.get("retrieval_score"),
                "text": str(chunk.get("text", ""))[:chunk_text_cap],
            }
        )

    user_obj = {
        "task": "generate_clarifying_questions_before_qemu_modeling",
        "device_name": analysis.get("device_name"),
        "device_type": analysis.get("device_type"),
        "priority_topics": [
            "W1C/W1S",
            "read-clear",
            "write-trigger",
            "busy timing",
            "FIFO depth/threshold",
            "IRQ set/clear and mask interaction",
            "polling timeout",
            "DMA descriptor layout",
        ],
        "derived_summary": derived,
        "question_budget": budget,
        "driver_evidence": driver_evidence,
        "reference_qemu_evidence": ref_evidence,
        "retrieved_code_chunks": chunk_payload,
        "output_schema": {
            "questions": [
                {
                    "question": "string",
                    "why": "string",
                    "answer_format": "one of object|string|number|array|boolean",
                    "examples": ["string", "string"],
                    "evidence_refs": ["path:line", "chunk-id"],
                }
            ]
        },
    }

    system = (
        "You are a senior QEMU peripheral modeling reviewer.\n"
        "Generate concrete clarifying questions based on provided driver/reference evidence.\n"
        "Rules:\n"
        "- Output STRICT JSON only. No markdown, no prose.\n"
        f"- Generate between {budget['min']} and {budget['max']} questions. Target is {budget['target']}.\n"
        "- If evidence already strongly determines a behavior, avoid redundant questions.\n"
        "- If evidence is missing, ask only high-value questions that unblock emulation behavior.\n"
        "- Questions must reference observed registers/access patterns, not generic templates.\n"
        "- Prioritize side-effect semantics: W1C/W1S, read-clear, write-trigger, busy timing, FIFO, IRQ, polling timeout, DMA descriptors.\n"
        "- Keep each question one sentence and directly answerable.\n"
        "- Do NOT output thinking/reasoning trace. Output final JSON directly.\n"
        "- Every question must include `evidence_refs` (path:line or chunk-id). Use ['missing-evidence'] if no direct reference exists.\n"
        "- Use practical answer_format and examples.\n"
    )
    user_content = json.dumps(user_obj, ensure_ascii=False, indent=2)
    if use_no_think:
        # qwen thinking models on ollama support this directive.
        user_content = "/no_think\n" + user_content
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


async def _generate_questions_via_llm(
    *,
    analysis: dict[str, Any],
    project_root: Path,
    llm_config_path: str | Path,
    top_k: int,
    temperature: float,
    max_tokens: int | None,
) -> dict[str, Any]:
    target = resolve_llm_target(llm_config_path)
    budget = _estimate_question_budget(analysis)
    initial_top_chunks = select_top_chunks(project_root, analysis, {}, top_k)
    use_no_think = target.protocol == "ollama" and target.model_name.lower().startswith("qwen3")
    messages = _build_question_prompt(
        analysis,
        initial_top_chunks,
        budget,
        compact=False,
        use_no_think=use_no_think,
    )

    parts: list[str] = []
    async for token in stream_chat_completion(
        target,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format="json",
    ):
        parts.append(token)
    raw = "".join(parts)
    retry_used = False
    retry_reason = None
    try:
        parsed = _extract_json_payload(raw)
    except ValueError as first_exc:
        retry_used = True
        retry_reason = str(first_exc)
        retry_top_k = max(4, min(top_k, top_k // 2 if top_k > 6 else top_k))
        retry_top_chunks = select_top_chunks(project_root, analysis, {}, retry_top_k)
        retry_messages = _build_question_prompt(
            analysis,
            retry_top_chunks,
            budget,
            compact=True,
            use_no_think=True if target.protocol == "ollama" else use_no_think,
        )
        retry_max_tokens = max((max_tokens or 0), 4096) if target.protocol == "ollama" else max_tokens
        retry_parts: list[str] = []
        async for token in stream_chat_completion(
            target,
            retry_messages,
            temperature=temperature,
            max_tokens=retry_max_tokens,
            response_format="json",
        ):
            retry_parts.append(token)
        retry_raw = "".join(retry_parts)
        try:
            parsed = _extract_json_payload(retry_raw)
            raw = retry_raw
            initial_top_chunks = retry_top_chunks
        except ValueError as second_exc:
            if target.protocol == "ollama":
                raise ValueError(
                    "LLM returned empty questions payload after retry. "
                    f"model={target.model_name}. "
                    "This commonly happens on tiny thinking models when token budget is spent on reasoning. "
                    "Try a stronger local model (e.g. qwen2.5-coder:14b), or increase question_max_tokens."
                ) from second_exc
            raise

    if isinstance(parsed, dict):
        raw_items = parsed.get("questions", [])
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raw_items = []

    items = _normalize_question_items(raw_items if isinstance(raw_items, list) else [], max_items=budget["max"])
    if len(items) < budget["min"]:
        raise ValueError(f"LLM generated too few valid questions: {len(items)} < {budget['min']}")
    items = items[: budget["max"]]

    return {
        "version": "mvp-2",
        "generated_at": utc_now_iso(),
        "source": "llm-analysis",
        "question_budget": budget,
        "llm": {
            "provider": target.provider_name,
            "model": target.model_name,
            "llm_config_path": str(llm_config_path),
            "token_chars": len(raw),
            "top_k_chunks": len(initial_top_chunks),
            "retry_used": retry_used,
            "retry_reason": retry_reason,
        },
        "questions": items,
    }


def generate_questions_from_analysis(analysis: dict[str, Any], project_root: Path) -> dict[str, Any]:
    # heuristic fallback implementation
    derived = analysis.get("derived", {})
    driver = analysis.get("driver", {})
    device_name = analysis.get("device_name", "device")
    budget = _estimate_question_budget(analysis)

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
    if derived.get("driver_mmio_access_count", 0) > 0:
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
    if derived.get("driver_polling_pattern_count", 0) > 0:
        add(
            "What is the BUSY/READY/DONE timing model after command start? Include delays/poll-count limits if known.",
            "Drivers often rely on exact status transitions in polling loops and timeout paths.",
            "object",
            ['{"BUSY": "set on START, clear after <=100 polls", "DONE": "set when BUSY clears"}'],
        )
        add(
            "What polling timeout behavior does the driver expect (delay function, timeout units, error code)?",
            "This guides the minimum state transition timing needed to avoid spurious timeouts in the guest.",
            "object",
            ['{"delay_us": 10, "timeout_us": 10000, "error": "-ETIMEDOUT"}'],
        )
    if irq_regs or driver.get("irq_clues"):
        add(
            "What are IRQ set/clear conditions? Please distinguish raw status, mask/enable, masked status, and line update logic.",
            "QEMU must implement correct qemu_set_irq behavior and status/mask interaction.",
            "object",
            ['{"set": ["RX_READY", "DMA_DONE"], "clear": ["W1C INT_STATUS"], "line_assert": "raw&mask != 0"}'],
        )
    if fifo_regs:
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
        "version": "mvp-2",
        "generated_at": utc_now_iso(),
        "source": "heuristic-analysis",
        "question_budget": budget,
        "questions": deduped[: budget["max"]],
    }

    json_dump(project_root / "artifacts" / "questions.json", result)
    return result


async def generate_questions(
    analysis: dict[str, Any],
    project_root: Path,
    *,
    llm_config_path: str | Path | None = None,
    use_llm: bool = True,
    allow_heuristic_fallback: bool = True,
    top_k: int = 10,
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    artifacts_path = project_root / "artifacts" / "questions.json"
    resolved_config = str(llm_config_path or DEFAULT_LLM_CONFIG_PATH)

    if use_llm:
        try:
            result = await _generate_questions_via_llm(
                analysis=analysis,
                project_root=project_root,
                llm_config_path=resolved_config,
                top_k=top_k,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            json_dump(artifacts_path, result)
            return result
        except (LLMConfigError, LLMRequestError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            if not allow_heuristic_fallback:
                raise RuntimeError(f"LLM question generation failed: {exc}") from exc
            fallback = generate_questions_from_analysis(analysis, project_root)
            fallback["source"] = "heuristic-fallback"
            fallback["fallback_reason"] = str(exc)
            fallback["llm_attempt"] = {
                "llm_config_path": resolved_config,
                "top_k_chunks": top_k,
            }
            json_dump(artifacts_path, fallback)
            return fallback

    return generate_questions_from_analysis(analysis, project_root)


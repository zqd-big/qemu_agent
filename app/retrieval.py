from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .utils import json_load, tokenize_keywords


def _candidate_terms(analysis: dict[str, Any], answers: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    parts.append(analysis.get("device_name", ""))
    parts.append(analysis.get("device_type", ""))
    derived = analysis.get("derived", {})
    for key in (
        "candidate_registers",
        "likely_irq_regs",
        "likely_status_regs",
        "likely_control_regs",
        "likely_fifo_regs",
        "likely_dma_regs",
    ):
        parts.extend(derived.get(key, [])[:50])
    parts.append(json.dumps(answers or {}, ensure_ascii=False))
    return tokenize_keywords(" ".join(parts))


def select_top_chunks(project_root: Path, analysis: dict[str, Any], answers: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
    chunks_obj = json_load(project_root / "artifacts" / "chunks.json")
    chunks: list[dict[str, Any]] = chunks_obj.get("chunks", [])
    if not chunks:
        return []

    term_freq = Counter(_candidate_terms(analysis, answers))
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk in chunks:
        text_terms = Counter(tokenize_keywords(chunk.get("text", "")))
        score = 0.0
        for t, w in term_freq.items():
            if t in text_terms:
                score += min(text_terms[t], 3) * min(w, 3)
        if chunk.get("chunk_type") == "function":
            score += 1.5
        if chunk.get("chunk_type") == "macro":
            score += 1.0
        if chunk.get("bucket") == "driver":
            score += 0.75
        if len(chunk.get("text", "")) < 5000:
            score += 0.25
        scored.append((score, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    buckets_seen = {"driver": 0, "reference": 0}
    for score, chunk in scored:
        if score <= 0 and len(selected) >= min(4, top_k):
            break
        selected.append({**chunk, "retrieval_score": round(score, 2)})
        if isinstance(chunk.get("id"), str):
            selected_ids.add(chunk["id"])
        buckets_seen[chunk.get("bucket", "driver")] = buckets_seen.get(chunk.get("bucket", "driver"), 0) + 1
        if len(selected) >= top_k:
            break

    # 灏介噺淇濊瘉 driver/reference 閮芥湁涓婁笅鏂?    if buckets_seen.get("reference", 0) == 0:
        for score, chunk in scored:
            if chunk.get("bucket") == "reference" and chunk.get("id") not in selected_ids:
                selected.append({**chunk, "retrieval_score": round(score, 2)})
                if isinstance(chunk.get("id"), str):
                    selected_ids.add(chunk["id"])
                break
    if buckets_seen.get("driver", 0) == 0:
        for score, chunk in scored:
            if chunk.get("bucket") == "driver" and chunk.get("id") not in selected_ids:
                selected.append({**chunk, "retrieval_score": round(score, 2)})
                if isinstance(chunk.get("id"), str):
                    selected_ids.add(chunk["id"])
                break

    return selected[:top_k]

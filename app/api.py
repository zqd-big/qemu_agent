from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import db
from .analyse import run_analysis
from .generator import build_generation_messages, build_report_markdown, sanitize_and_validate_c_output
from .ingest import run_ingest
from .llm_client import LLMConfigError, LLMRequestError, resolve_llm_target, stream_chat_completion
from .models import AnalysisResponse, CreateProjectRequest, GenerateRequest, ProjectResponse, SaveAnswersRequest
from .questions import generate_questions_from_analysis
from .retrieval import select_top_chunks
from .settings import DEFAULT_LLM_CONFIG_PATH, MAX_UPLOAD_MB, PROJECTS_DIR
from .utils import ensure_dir, json_dump, json_load, safe_slug, sanitize_artifact_name, utc_now_iso


router = APIRouter()


def _project_root(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def _project_meta_or_404(project_id: str) -> dict[str, Any]:
    meta = db.get_project(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    return meta


def _write_project_meta_file(project_root: Path, meta: dict[str, Any]) -> None:
    json_dump(project_root / "project.json", meta)


def _clear_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


async def _save_upload(upload: UploadFile, dest: Path) -> int:
    ensure_dir(dest.parent)
    size = 0
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"Upload too large (> {MAX_UPLOAD_MB} MB): {upload.filename}")
            f.write(chunk)
    await upload.close()
    return size


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(zip_path):
        raise HTTPException(status_code=400, detail=f"Unsupported archive (zip only in MVP): {zip_path.name}")
    _clear_dir(dest_dir)
    extracted_files = 0
    extracted_dirs = 0
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            member_name = info.filename.replace("\\", "/")
            if not member_name or member_name.startswith("/"):
                raise HTTPException(status_code=400, detail=f"Unsafe zip member path: {member_name!r}")
            target = (dest_dir / member_name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Zip path traversal detected: {member_name}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                extracted_dirs += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            extracted_files += 1
    return {"files": extracted_files, "dirs": extracted_dirs}


def _count_real_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file())


def _load_required_artifact(project_root: Path, name: str) -> Any:
    path = project_root / "artifacts" / name
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Missing artifact: {name}. Run previous stage first.")
    return json_load(path)


def _resolve_config_path(raw: str | None) -> Path:
    if not raw:
        return DEFAULT_LLM_CONFIG_PATH
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def _ndjson_event(event_type: str, **payload: Any) -> bytes:
    obj = {"type": event_type, **payload}
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": utc_now_iso()}


@router.get("/projects")
def list_projects() -> list[dict[str, Any]]:
    return db.list_projects()


@router.post("/projects", response_model=ProjectResponse)
def create_project(req: CreateProjectRequest) -> ProjectResponse:
    device_name_slug = safe_slug(req.device_name, "device")
    project_id = f"{device_name_slug}-{utc_now_iso().replace(':', '').replace('-', '').replace('+00:00', 'z').replace('.', '')}"
    root = _project_root(project_id)
    if root.exists():
        raise HTTPException(status_code=409, detail=f"Project directory already exists: {project_id}")

    ensure_dir(root)
    ensure_dir(root / "uploads" / "raw")
    ensure_dir(root / "uploads" / "driver")
    ensure_dir(root / "uploads" / "reference")
    ensure_dir(root / "artifacts")

    db.create_project(project_id, req.device_name, req.device_type, str(root))
    meta = db.get_project(project_id)
    if meta:
        _write_project_meta_file(root, meta)

    return ProjectResponse(
        id=project_id,
        device_name=req.device_name,
        device_type=req.device_type,
        status="created",
    )


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    meta = _project_meta_or_404(project_id)
    root = _project_root(project_id)
    artifacts = []
    artifacts_dir = root / "artifacts"
    if artifacts_dir.exists():
        artifacts = sorted([p.name for p in artifacts_dir.iterdir() if p.is_file()])
    return {**meta, "artifacts": artifacts}


@router.post("/projects/{project_id}/upload")
async def upload_archives(
    project_id: str,
    driver_archive: UploadFile | None = File(default=None),
    reference_archive: UploadFile | None = File(default=None),
    note: str | None = Form(default=None),
) -> dict[str, Any]:
    _project_meta_or_404(project_id)
    if not driver_archive and not reference_archive:
        raise HTTPException(status_code=400, detail="At least one of driver_archive or reference_archive is required")

    root = _project_root(project_id)
    raw_dir = ensure_dir(root / "uploads" / "raw")
    results: dict[str, Any] = {"project_id": project_id, "note": note, "uploaded": {}}

    if driver_archive:
        raw_path = raw_dir / "driver.zip"
        size = await _save_upload(driver_archive, raw_path)
        extracted = _safe_extract_zip(raw_path, root / "uploads" / "driver")
        results["uploaded"]["driver"] = {"raw": raw_path.name, "size_bytes": size, **extracted}
    if reference_archive:
        raw_path = raw_dir / "reference.zip"
        size = await _save_upload(reference_archive, raw_path)
        extracted = _safe_extract_zip(raw_path, root / "uploads" / "reference")
        results["uploaded"]["reference"] = {"raw": raw_path.name, "size_bytes": size, **extracted}

    db.update_project_status(project_id, "uploaded")
    return results


@router.post("/projects/{project_id}/analyse", response_model=AnalysisResponse)
def analyse_project(project_id: str) -> AnalysisResponse:
    meta = _project_meta_or_404(project_id)
    root = _project_root(project_id)
    if _count_real_files(root / "uploads" / "driver") == 0:
        raise HTTPException(status_code=400, detail="Driver upload is empty. Upload driver archive first.")
    if _count_real_files(root / "uploads" / "reference") == 0:
        raise HTTPException(status_code=400, detail="Reference QEMU upload is empty. Upload reference archive first.")

    db.update_project_status(project_id, "analysing")
    try:
        ingest_out = run_ingest(root)
        analysis = run_analysis(root, meta["device_name"], meta["device_type"])
        questions = generate_questions_from_analysis(analysis, root)
        db.update_project_status(project_id, "questions_ready")
        summary = {
            "files": ingest_out["ingest"]["file_count"],
            "chunks": ingest_out["ingest"]["chunks_count"],
            "candidate_registers": len(analysis.get("derived", {}).get("candidate_registers", [])),
            "questions": len(questions.get("questions", [])),
        }
        return AnalysisResponse(
            project_id=project_id,
            analysis_artifact="analysis.json",
            questions_artifact="questions.json",
            summary=summary,
        )
    except HTTPException:
        db.update_project_status(project_id, "analyse_failed")
        raise
    except Exception as exc:
        db.update_project_status(project_id, "analyse_failed")
        raise HTTPException(status_code=500, detail=f"Analyse failed: {exc}") from exc


@router.get("/projects/{project_id}/questions")
def get_questions(project_id: str) -> Any:
    _project_meta_or_404(project_id)
    root = _project_root(project_id)
    path = root / "artifacts" / "questions.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="questions.json not found. Run analyse first.")
    return JSONResponse(content=json_load(path))


@router.post("/projects/{project_id}/answers")
def save_answers(project_id: str, req: SaveAnswersRequest) -> dict[str, Any]:
    _project_meta_or_404(project_id)
    root = _project_root(project_id)
    artifacts_dir = ensure_dir(root / "artifacts")
    json_dump(artifacts_dir / "answers.json", req.answers)
    db.save_project_answers(project_id, req.answers)
    db.update_project_status(project_id, "answers_ready")
    return {"project_id": project_id, "saved": True, "artifact": "answers.json", "answer_count": len(req.answers)}


@router.post("/projects/{project_id}/generate")
async def generate_c_code(
    project_id: str,
    req: GenerateRequest | None = Body(default=None),
) -> StreamingResponse:
    meta = _project_meta_or_404(project_id)
    root = _project_root(project_id)
    request = req or GenerateRequest()
    artifacts_dir = ensure_dir(root / "artifacts")

    analysis_path = artifacts_dir / "analysis.json"
    answers_path = artifacts_dir / "answers.json"
    if not analysis_path.exists():
        raise HTTPException(status_code=400, detail="Missing analysis.json. Run /analyse first.")
    if not answers_path.exists():
        raise HTTPException(status_code=400, detail="Missing answers.json. Save answers first.")

    analysis = json_load(analysis_path)
    answers = json_load(answers_path)
    top_chunks = select_top_chunks(root, analysis, answers, request.top_k)
    messages = build_generation_messages(
        device_name=meta["device_name"],
        device_type=meta["device_type"],
        analysis=analysis,
        answers=answers,
        top_chunks=top_chunks,
    )

    config_path = _resolve_config_path(request.llm_config_path)
    try:
        target = resolve_llm_target(config_path)
    except LLMConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    c_file_name = f"{safe_slug(meta['device_name'], 'device')}.c"
    c_file_path = artifacts_dir / c_file_name
    report_path = artifacts_dir / "report.md"

    async def event_stream() -> AsyncIterator[bytes]:
        raw_parts: list[str] = []
        db.update_project_status(project_id, "generating")
        yield _ndjson_event(
            "status",
            message="generation_started",
            provider=target.provider_name,
            model=target.model_name,
            llm_config_path=str(config_path),
        )
        yield _ndjson_event("status", message="streaming_tokens")

        try:
            async for token in stream_chat_completion(
                target,
                messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            ):
                raw_parts.append(token)
                yield _ndjson_event("token", text=token)
                # cooperative scheduling for long streams
                await asyncio.sleep(0)

            raw_text = "".join(raw_parts)
            c_text = sanitize_and_validate_c_output(raw_text)
            c_file_path.write_text(c_text, encoding="utf-8")

            report_file = None
            if request.generate_report:
                report_md = build_report_markdown(
                    project_id=project_id,
                    device_name=meta["device_name"],
                    device_type=meta["device_type"],
                    analysis=analysis,
                    answers=answers,
                    selected_chunks=top_chunks,
                    c_artifact_name=c_file_name,
                )
                report_path.write_text(report_md, encoding="utf-8")
                report_file = report_path.name

            db.record_generation(
                project_id=project_id,
                provider_name=target.provider_name,
                model_name=target.model_name,
                artifact_path=str(c_file_path),
                report_path=str(report_path) if request.generate_report else None,
                prompt_meta={"top_k": request.top_k, "chunk_count": len(top_chunks), "llm_config_path": str(config_path)},
                answers=answers,
                success=True,
            )
            db.update_project_status(project_id, "generated")
            yield _ndjson_event(
                "done",
                message="generation_completed",
                artifact=c_file_name,
                report=report_file,
                token_chars=len(c_text),
            )
        except (LLMRequestError, ValueError) as exc:
            db.record_generation(
                project_id=project_id,
                provider_name=target.provider_name,
                model_name=target.model_name,
                artifact_path=None,
                report_path=None,
                prompt_meta={"top_k": request.top_k, "chunk_count": len(top_chunks), "llm_config_path": str(config_path)},
                answers=answers,
                success=False,
                error=str(exc),
            )
            db.update_project_status(project_id, "generate_failed")
            yield _ndjson_event("error", message=str(exc))
        except Exception as exc:
            db.record_generation(
                project_id=project_id,
                provider_name=target.provider_name,
                model_name=target.model_name,
                artifact_path=None,
                report_path=None,
                prompt_meta={"top_k": request.top_k, "chunk_count": len(top_chunks), "llm_config_path": str(config_path)},
                answers=answers,
                success=False,
                error=f"Unexpected: {exc}",
            )
            db.update_project_status(project_id, "generate_failed")
            yield _ndjson_event("error", message=f"Unexpected generation failure: {exc}")

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@router.get("/projects/{project_id}/artifact/{name}")
def get_artifact(project_id: str, name: str):
    _project_meta_or_404(project_id)
    root = _project_root(project_id)
    try:
        safe_name = sanitize_artifact_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    path = root / "artifacts" / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Artifact not found: {safe_name}")
    media = "application/octet-stream"
    if safe_name.endswith(".json"):
        media = "application/json"
    elif safe_name.endswith(".md"):
        media = "text/markdown; charset=utf-8"
    elif safe_name.endswith(".c") or safe_name.endswith(".h"):
        media = "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=safe_name)

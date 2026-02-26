from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    device_name: str = Field(..., min_length=1, max_length=128)
    device_type: str = Field(..., min_length=1, max_length=32)


class ProjectResponse(BaseModel):
    id: str
    device_name: str
    device_type: str
    status: str


class SaveAnswersRequest(BaseModel):
    answers: dict[str, Any]


class GenerateRequest(BaseModel):
    llm_config_path: str | None = None
    top_k: int = Field(default=12, ge=1, le=50)
    generate_report: bool = True
    temperature: float = Field(default=0.1, ge=0.0, le=1.5)
    max_tokens: int | None = Field(default=None, ge=128, le=32768)


class AnalysisResponse(BaseModel):
    project_id: str
    analysis_artifact: str
    questions_artifact: str
    summary: dict[str, Any]


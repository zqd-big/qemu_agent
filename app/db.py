from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .settings import DB_PATH
from .utils import ensure_dir, utc_now_iso


def _connect() -> sqlite3.Connection:
    ensure_dir(Path(DB_PATH).parent)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                device_type TEXT NOT NULL,
                root_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                answers_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                provider_name TEXT,
                model_name TEXT,
                artifact_path TEXT,
                report_path TEXT,
                prompt_meta_json TEXT,
                answers_json TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def create_project(project_id: str, device_name: str, device_type: str, root_path: str) -> None:
    now = utc_now_iso()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO projects (id, device_name, device_type, root_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'created', ?, ?)
            """,
            (project_id, device_name, device_type, root_path, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_project(project_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_projects() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_project_status(project_id: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now_iso(), project_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_project_answers(project_id: str, answers: dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE projects SET answers_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(answers, ensure_ascii=False), utc_now_iso(), project_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_generation(
    project_id: str,
    provider_name: str | None,
    model_name: str | None,
    artifact_path: str | None,
    report_path: str | None,
    prompt_meta: dict[str, Any] | None,
    answers: dict[str, Any] | None,
    success: bool,
    error: str | None = None,
) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO generations (
                project_id, provider_name, model_name, artifact_path, report_path,
                prompt_meta_json, answers_json, success, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                provider_name,
                model_name,
                artifact_path,
                report_path,
                json.dumps(prompt_meta or {}, ensure_ascii=False),
                json.dumps(answers or {}, ensure_ascii=False),
                1 if success else 0,
                error,
                utc_now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


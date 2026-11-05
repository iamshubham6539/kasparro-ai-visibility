import json
import os
import secrets
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

import analyze
import db
import llm

app = FastAPI(
    title="Kasparro AI visibility backend",
    version="1.0.0",
    description="Analyse saved AI answers, score visibility and enforce report review.",
)

DIAGNOSTIC_KEYS = ("observation", "possible_explanation", "test_evidence", "response_ids", "excerpts")
REMEDY_KEYS = ("problem", "evidence", "action", "benefit", "effort", "uncertainty", "validation_step")


def pack_dir():
    return os.getenv("KASPARRO_PACK_DIR", db.BASE_DIR)


def api_error(status_code, code, message, details=None):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": str(exc.detail), "details": {}}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request body failed validation",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )

@app.exception_handler(Exception)
async def unhandled_error_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Unexpected server error", "details": {}}},
    )




def configured_tokens():
    analyst = os.getenv("ANALYST_TOKEN", "analyst-dev-token")
    reviewer = os.getenv("REVIEWER_TOKEN", "reviewer-dev-token")
    if analyst == reviewer:
        raise RuntimeError("ANALYST_TOKEN and REVIEWER_TOKEN must be different")
    return analyst, reviewer


bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Paste the analyst or reviewer dev token. Swagger adds the 'Bearer ' prefix.",
)


def role_from_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)):
    if credentials is None or credentials.scheme.lower() != "bearer":
        api_error(401, "missing_token", "Send an Authorization: Bearer <token> header")
    token = credentials.credentials.strip()
    analyst, reviewer = configured_tokens()
    if secrets.compare_digest(token, analyst):
        return "analyst"
    if secrets.compare_digest(token, reviewer):
        return "reviewer"
    api_error(401, "invalid_token", "The supplied token is not valid")


def require_analyst(role=Depends(role_from_token)):
    if role != "analyst":
        api_error(403, "forbidden_role", "This action requires the analyst token")
    return role


def require_reviewer(role=Depends(role_from_token)):
    if role != "reviewer":
        api_error(403, "forbidden_role", "This action requires the reviewer token")
    return role


class AnalysisRequest(BaseModel):
    dataset_id: str | None = None
    model_config = ConfigDict(extra="forbid")


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(min_length=1, max_length=2000)
    model_config = ConfigDict(extra="forbid")


class ReportPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=5000)
    diagnostics: list[dict] | None = None
    remedies: list[dict] | None = None
    notes: str | None = Field(default=None, max_length=5000)
    model_config = ConfigDict(extra="forbid")


def check_diagnostics(items):
    if len(items) != 3:
        api_error(422, "invalid_diagnostics", "A report must contain exactly 3 diagnostics")
    for item in items:
        if not isinstance(item, dict):
            api_error(422, "invalid_diagnostics", "Each diagnostic must be an object")
        for key in DIAGNOSTIC_KEYS:
            if key not in item:
                api_error(422, "invalid_diagnostics", "Diagnostic is missing a required field", {"field": key})
        for key in ("observation", "possible_explanation", "test_evidence"):
            if not isinstance(item[key], str):
                api_error(422, "invalid_diagnostics", "Diagnostic text fields must be strings", {"field": key})
        if not isinstance(item["response_ids"], list) or not isinstance(item["excerpts"], list):
            api_error(422, "invalid_diagnostics", "Diagnostic response_ids and excerpts must be lists")
        if any(not isinstance(response_id, str) for response_id in item["response_ids"]):
            api_error(422, "invalid_diagnostics", "Diagnostic response_ids must be strings")


def check_remedies(items):
    if len(items) != 3:
        api_error(422, "invalid_remedies", "A report must contain exactly 3 remedies")
    for item in items:
        if not isinstance(item, dict):
            api_error(422, "invalid_remedies", "Each remedy must be an object")
        for key in REMEDY_KEYS:
            if key not in item:
                api_error(422, "invalid_remedies", "Remedy is missing a required field", {"field": key})
            if not isinstance(item[key], str):
                api_error(422, "invalid_remedies", "Remedy fields must be strings", {"field": key})


def apply_patch(content, patch):
    updated = json.loads(json.dumps(content))
    if patch.title is not None:
        updated["title"] = patch.title
    if patch.summary is not None:
        updated["summary"] = patch.summary
    if patch.notes is not None:
        updated["notes"] = patch.notes
    if patch.diagnostics is not None:
        check_diagnostics(patch.diagnostics)
        updated["diagnostics"] = patch.diagnostics
    if patch.remedies is not None:
        check_remedies(patch.remedies)
        updated["remedies"] = patch.remedies
    return updated


def report_status(conn, report_id):
    summary = db.report_summary(conn, report_id)
    if summary is None:
        api_error(404, "report_not_found", "Report not found", {"report_id": report_id})
    return summary


@app.get("/health")
def health():
    try:
        conn = db.init_db()
        conn.execute("SELECT 1")
        counts = {
            "datasets": conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0],
            "analyses": conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],
            "reports": conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        }
        conn.close()
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "database_unavailable", "message": str(exc), "details": {}}},
        )
    return {
        "status": "ok",
        "database": "ok",
        "execution_mode": os.getenv("KASPARRO_LLM_MODE", "replay").strip().lower(),
        "score_version": analyze.SCORE_VERSION,
        "category_version": analyze.CATEGORY_VERSION,
        "counts": counts,
    }


@app.post("/datasets")
def import_dataset(role=Depends(require_analyst)):
    try:
        result = db.import_pack(pack_dir(), analyze.categorize_prompt)
    except db.PackError as exc:
        api_error(exc.status_code, exc.code, exc.message, exc.details)
    status_code = 201 if result["created"] else 200
    return JSONResponse(status_code=status_code, content=result)


@app.post("/analyses")
def create_analysis(body: AnalysisRequest | None = None, role=Depends(require_analyst)):
    conn = db.init_db()
    dataset_id = body.dataset_id if body is not None else None
    if dataset_id:
        dataset = db.get_dataset(conn, dataset_id)
    else:
        dataset = db.get_latest_dataset(conn)
    if dataset is None:
        conn.close()
        api_error(404, "dataset_not_found", "Import a dataset before running an analysis")

    try:
        mode = llm.current_mode()
    except llm.ModelError as exc:
        conn.close()
        api_error(422, "invalid_execution_mode", exc.message)

    analysis_id = db.create_analysis(conn, dataset["dataset_id"], mode, analyze.SCORE_VERSION, analyze.CATEGORY_VERSION)
    try:
        outcome = analyze.run_analysis(conn, dataset["dataset_id"], analysis_id, mode)
    except Exception as exc:
        conn.close()
        api_error(500, "analysis_failed", "Analysis failed before completion", {"analysis_id": analysis_id, "error": str(exc)})

    try:
        report_id = db.create_report(conn, analysis_id, outcome["report"], "analyst")
    except Exception as exc:
        conn.close()
        api_error(500, "report_creation_failed", "Analysis completed but the report could not be saved", {"analysis_id": analysis_id, "error": str(exc)})

    conn.close()
    return JSONResponse(
        status_code=201,
        content={
            "analysis_id": analysis_id,
            "status": "completed",
            "execution_mode": mode,
            "counts": outcome["counts"],
            "report_id": report_id,
        },
    )


@app.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str, role=Depends(role_from_token)):
    conn = db.init_db()
    analysis = db.get_analysis(conn, analysis_id)
    if analysis is None:
        conn.close()
        api_error(404, "analysis_not_found", "Analysis not found", {"analysis_id": analysis_id})
    content = {
        "analysis_id": analysis["analysis_id"],
        "dataset_id": analysis["dataset_id"],
        "report_id": analysis["report_id"],
        "status": analysis["status"],
        "mode": analysis["mode"],
        "score_version": analysis["score_version"],
        "category_version": analysis["category_version"],
        "created_at": analysis["created_at"],
        "started_at": analysis["started_at"],
        "finished_at": analysis["finished_at"],
        "counts": {
            "attempted": analysis["total"],
            "valid": analysis["valid"],
            "failed": analysis["failed"],
            "skipped": analysis["skipped"],
        },
        "error": analysis["error"],
    }
    conn.close()
    return content


@app.get("/reports/{report_id}")
def get_report(report_id: str, role=Depends(role_from_token)):
    conn = db.init_db()
    summary = report_status(conn, report_id)
    conn.close()
    return summary


@app.patch("/reports/{report_id}")
def edit_report(report_id: str, patch: ReportPatch, role=Depends(require_analyst)):
    if not any(value is not None for value in (patch.title, patch.summary, patch.diagnostics, patch.remedies, patch.notes)):
        api_error(422, "empty_patch", "Provide at least one editable field")
    conn = db.init_db()
    report = db.get_report(conn, report_id)
    if report is None:
        conn.close()
        api_error(404, "report_not_found", "Report not found", {"report_id": report_id})
    version = db.get_report_version(conn, report_id, report["current_version"])
    content = json.loads(version["content_json"])
    updated = apply_patch(content, patch)
    if version["status"] == "draft":
        db.update_draft_content(conn, report_id, report["current_version"], updated)
        version_number = report["current_version"]
    else:
        version_number = db.add_report_version(conn, report_id, updated, role)
    conn.close()
    return {
        "report_id": report_id,
        "version_number": version_number,
        "status": "draft",
        "message": "Edit saved as a draft and requires review before export",
    }


@app.post("/reports/{report_id}/submit")
def submit_report(report_id: str, role=Depends(require_analyst)):
    conn = db.init_db()
    report = db.get_report(conn, report_id)
    if report is None:
        conn.close()
        api_error(404, "report_not_found", "Report not found", {"report_id": report_id})
    version = db.get_report_version(conn, report_id, report["current_version"])
    if version["status"] != "draft":
        conn.close()
        api_error(409, "not_draft", "Only a draft version can be submitted", {"status": version["status"]})
    db.submit_version(conn, report_id, report["current_version"], role)
    summary = db.report_summary(conn, report_id)
    conn.close()
    return summary


@app.post("/reports/{report_id}/decision")
def decide_report(report_id: str, body: DecisionRequest, role=Depends(require_reviewer)):
    conn = db.init_db()
    report = db.get_report(conn, report_id)
    if report is None:
        conn.close()
        api_error(404, "report_not_found", "Report not found", {"report_id": report_id})
    version = db.get_report_version(conn, report_id, report["current_version"])
    if version["status"] != "submitted":
        conn.close()
        api_error(409, "not_submitted", "Only a submitted version can be approved or rejected", {"status": version["status"]})
    db.decide_version(conn, report_id, report["current_version"], body.decision, role, body.comment)
    summary = db.report_summary(conn, report_id)
    conn.close()
    return summary


@app.get("/reports/{report_id}/export")
def export_report(
    report_id: str,
    version: int | None = Query(default=None, ge=1),
    role=Depends(role_from_token),
):
    conn = db.init_db()
    report = db.get_report(conn, report_id)
    if report is None:
        conn.close()
        api_error(404, "report_not_found", "Report not found", {"report_id": report_id})
    explicit_version = version is not None
    if version is None:
        version = report["current_version"]
    row = db.get_report_version(conn, report_id, version)
    if row is None:
        conn.close()
        api_error(404, "version_not_found", "Report version not found", {"version": version})
    if row["status"] != "approved":
        conn.close()
        api_error(
            409,
            "export_not_approved",
            "Only an approved version can be exported",
            {"version": version, "status": row["status"]},
        )
    content = json.loads(row["content_json"])
    conn.close()
    return JSONResponse(
        status_code=200,
        headers={"X-Report-Version": str(version)},
        content={
            "report_id": report_id,
            "version_number": version,
            "status": "approved",
            "is_current": version == report["current_version"],
            "explicit_version": explicit_version,
            "exported_at": db.utc_now(),
            "content": content,
        },
    )

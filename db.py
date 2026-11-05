import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "kasparro.db")


class PackError(Exception):
    def __init__(self, status_code, code, message, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path():
    return os.getenv("KASPARRO_DB", DEFAULT_DB)


def connect(path=None):
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL,
    prompt_count INTEGER NOT NULL,
    response_count INTEGER NOT NULL,
    citation_count INTEGER NOT NULL,
    raw_run_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    run_id TEXT NOT NULL,
    text TEXT NOT NULL,
    branded INTEGER NOT NULL,
    category TEXT NOT NULL,
    category_version TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS responses (
    response_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    run_id TEXT NOT NULL,
    prompt_id TEXT NOT NULL REFERENCES prompts(prompt_id),
    engine TEXT NOT NULL,
    engine_name TEXT,
    model_version TEXT,
    answer_text TEXT NOT NULL,
    answer_markdown TEXT,
    capture_status TEXT NOT NULL,
    provider_timestamp TEXT,
    archive_stored_at TEXT,
    settings_json TEXT NOT NULL,
    capture_route TEXT,
    provider_api_version TEXT,
    provider_task_id TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS citations (
    citation_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    response_id TEXT NOT NULL REFERENCES responses(response_id),
    prompt_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    order_no INTEGER NOT NULL,
    citation_type TEXT NOT NULL,
    url TEXT NOT NULL,
    url_normalized TEXT NOT NULL,
    domain TEXT,
    title TEXT,
    source_name TEXT,
    publication_date TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_citations_response ON citations(response_id);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    report_id TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    score_version TEXT NOT NULL,
    category_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    valid INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS response_results (
    result_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
    response_id TEXT NOT NULL REFERENCES responses(response_id),
    status TEXT NOT NULL,
    reason TEXT,
    mentioned INTEGER NOT NULL DEFAULT 0,
    recommended INTEGER NOT NULL DEFAULT 0,
    negative INTEGER NOT NULL DEFAULT 0,
    position INTEGER,
    competitors_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    citation_count_raw INTEGER NOT NULL DEFAULT 0,
    citation_count_deduped INTEGER NOT NULL DEFAULT 0,
    brand_citation_count INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(analysis_id, response_id)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_versions (
    report_id TEXT NOT NULL REFERENCES reports(report_id),
    version_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    submitted_by TEXT,
    submitted_at TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_comment TEXT,
    PRIMARY KEY (report_id, version_number)
);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(report_id),
    version_number INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);
"""


def init_db(path=None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def normalise_url(url):
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_"):
            continue
        if low in ("ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid"):
            continue
        query.append((key, value))
    query.sort()
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def url_host(url):
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _read_jsonl(path):
    if not os.path.exists(path):
        raise PackError(422, "pack_file_missing", "Required pack file is missing", {"path": path})
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise PackError(
                    422,
                    "invalid_jsonl",
                    "Could not parse a JSONL line",
                    {"path": path, "line": line_number, "error": exc.msg},
                )
    return records


def _raw(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _validate_records(records, definition, kind, path):
    try:
        import jsonschema
    except ImportError:
        raise PackError(500, "validator_missing", "jsonschema is required to validate input")

    validator = jsonschema.Draft202012Validator(definition)
    for index, record in enumerate(records):
        errors = sorted(validator.iter_errors(record), key=lambda item: list(item.path))
        if errors:
            first = errors[0]
            field = ".".join(str(part) for part in first.path) or "<record>"
            raise PackError(
                422,
                "schema_validation_failed",
                "Input record does not match the pack schema",
                {"kind": kind, "path": path, "index": index, "field": field, "error": first.message},
            )


def _check_unique(records, key, kind):
    seen = {}
    for index, record in enumerate(records):
        record_id = record[key]
        raw = _raw(record)
        if record_id in seen:
            raise PackError(
                409,
                "duplicate_in_file",
                "The same ID appears more than once in one input file",
                {"kind": kind, "id": record_id, "index": index},
            )
        seen[record_id] = raw


def _reuse_or_conflict(conn, table, id_column, record_id, raw, dataset_id):
    row = conn.execute(
        "SELECT dataset_id, raw_json FROM " + table + " WHERE " + id_column + " = ?",
        (record_id,),
    ).fetchone()
    if row is None:
        return False
    if row["dataset_id"] != dataset_id or row["raw_json"] != raw:
        raise PackError(
            409,
            "duplicate_conflict",
            "An existing record has the same ID but different content",
            {"table": table, "id": record_id},
        )
    return True


def import_pack(pack_dir, categorize):
    run_path = os.path.join(pack_dir, "data", "run.json")
    schema_path = os.path.join(pack_dir, "schemas", "input_contract.json")
    if not os.path.exists(run_path):
        raise PackError(422, "pack_file_missing", "data/run.json is missing", {"path": run_path})
    if not os.path.exists(schema_path):
        raise PackError(422, "pack_file_missing", "schemas/input_contract.json is missing", {"path": schema_path})

    with open(run_path, "r", encoding="utf-8") as handle:
        run = json.load(handle)
    with open(schema_path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)

    prompts = _read_jsonl(os.path.join(pack_dir, "data", "prompts.jsonl"))
    responses = _read_jsonl(os.path.join(pack_dir, "data", "responses.jsonl"))
    citations = _read_jsonl(os.path.join(pack_dir, "data", "citations.jsonl"))

    _validate_records(prompts, schema["definitions"]["prompt"], "prompt", "prompts.jsonl")
    _validate_records(responses, schema["definitions"]["response"], "response", "responses.jsonl")
    _validate_records(citations, schema["definitions"]["citation"], "citation", "citations.jsonl")

    _check_unique(prompts, "prompt_id", "prompt")
    _check_unique(responses, "response_id", "response")
    _check_unique(citations, "citation_id", "citation")

    run_id = run.get("run_id")
    if not run_id:
        raise PackError(422, "run_id_missing", "data/run.json has no run_id")

    prompt_map = {record["prompt_id"]: record for record in prompts}
    response_map = {record["response_id"]: record for record in responses}

    for record in prompts:
        if record["run_id"] != run_id:
            raise PackError(422, "run_id_mismatch", "Prompt belongs to a different run", {"prompt_id": record["prompt_id"]})
    for record in responses:
        if record["run_id"] != run_id:
            raise PackError(422, "run_id_mismatch", "Response belongs to a different run", {"response_id": record["response_id"]})
        if record["prompt_id"] not in prompt_map:
            raise PackError(422, "unknown_prompt", "Response points to an unknown prompt", {"response_id": record["response_id"]})
    for record in citations:
        response = response_map.get(record["response_id"])
        if response is None:
            raise PackError(422, "unknown_response", "Citation points to an unknown response", {"citation_id": record["citation_id"]})
        if record["prompt_id"] != response["prompt_id"]:
            raise PackError(422, "citation_prompt_mismatch", "Citation prompt does not match its response", {"citation_id": record["citation_id"]})
        if record["engine"] != response["engine"]:
            raise PackError(422, "citation_engine_mismatch", "Citation engine does not match its response", {"citation_id": record["citation_id"]})

    conn = init_db()
    dataset_id = str(uuid.uuid4())
    created = True
    reused = {"prompts": 0, "responses": 0, "citations": 0}
    try:
        existing_dataset = conn.execute("SELECT * FROM datasets WHERE run_id = ?", (run_id,)).fetchone()
        if existing_dataset is not None:
            dataset_id = existing_dataset["dataset_id"]
            created = False
        else:
            conn.execute(
                "INSERT INTO datasets (dataset_id, run_id, imported_at, prompt_count, response_count, citation_count, raw_run_json) VALUES (?, ?, ?, 0, 0, 0, ?)",
                (dataset_id, run_id, utc_now(), _raw(run)),
            )

        for record in prompts:
            raw = _raw(record)
            if _reuse_or_conflict(conn, "prompts", "prompt_id", record["prompt_id"], raw, dataset_id):
                reused["prompts"] += 1
                continue
            info = categorize(record["prompt_id"], record["text"])
            conn.execute(
                "INSERT INTO prompts (prompt_id, dataset_id, run_id, text, branded, category, category_version, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["prompt_id"],
                    dataset_id,
                    record["run_id"],
                    record["text"],
                    1 if info["branded"] else 0,
                    info["category"],
                    info["version"],
                    raw,
                ),
            )

        for record in responses:
            raw = _raw(record)
            if _reuse_or_conflict(conn, "responses", "response_id", record["response_id"], raw, dataset_id):
                reused["responses"] += 1
                continue
            conn.execute(
                """INSERT INTO responses (
                    response_id, dataset_id, run_id, prompt_id, engine, engine_name, model_version,
                    answer_text, answer_markdown, capture_status, provider_timestamp, archive_stored_at,
                    settings_json, capture_route, provider_api_version, provider_task_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["response_id"],
                    dataset_id,
                    record["run_id"],
                    record["prompt_id"],
                    record["engine"],
                    record.get("engine_name"),
                    record.get("model_version"),
                    record["answer_text"],
                    record.get("answer_markdown"),
                    record["capture_status"],
                    record.get("provider_timestamp"),
                    record.get("archive_stored_at"),
                    json.dumps(record.get("settings") or {}, sort_keys=True),
                    record.get("capture_route"),
                    record.get("provider_api_version"),
                    record.get("provider_task_id"),
                    raw,
                ),
            )

        for record in citations:
            raw = _raw(record)
            if _reuse_or_conflict(conn, "citations", "citation_id", record["citation_id"], raw, dataset_id):
                reused["citations"] += 1
                continue
            conn.execute(
                """INSERT INTO citations (
                    citation_id, dataset_id, response_id, prompt_id, engine, order_no, citation_type,
                    url, url_normalized, domain, title, source_name, publication_date, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["citation_id"],
                    dataset_id,
                    record["response_id"],
                    record["prompt_id"],
                    record["engine"],
                    record["order"],
                    record["citation_type"],
                    record["url"],
                    normalise_url(record["url"]),
                    record.get("domain") or url_host(record["url"]),
                    record.get("title"),
                    record.get("source_name"),
                    record.get("publication_date"),
                    raw,
                ),
            )

        counts = {
            "prompts": conn.execute("SELECT COUNT(*) FROM prompts WHERE dataset_id = ?", (dataset_id,)).fetchone()[0],
            "responses": conn.execute("SELECT COUNT(*) FROM responses WHERE dataset_id = ?", (dataset_id,)).fetchone()[0],
            "citations": conn.execute("SELECT COUNT(*) FROM citations WHERE dataset_id = ?", (dataset_id,)).fetchone()[0],
        }
        conn.execute(
            "UPDATE datasets SET imported_at = ?, prompt_count = ?, response_count = ?, citation_count = ? WHERE dataset_id = ?",
            (utc_now(), counts["prompts"], counts["responses"], counts["citations"], dataset_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    conn.close()
    return {
        "dataset_id": dataset_id,
        "run_id": run_id,
        "created": created,
        "reused_records": reused,
        "counts": counts,
    }


def get_dataset(conn, dataset_id):
    return conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()


def get_latest_dataset(conn):
    return conn.execute("SELECT * FROM datasets ORDER BY imported_at DESC LIMIT 1").fetchone()


def load_records(conn, dataset_id):
    dataset = get_dataset(conn, dataset_id)
    if dataset is None:
        return None
    prompt_rows = conn.execute("SELECT * FROM prompts WHERE dataset_id = ? ORDER BY prompt_id", (dataset_id,)).fetchall()
    response_rows = conn.execute("SELECT * FROM responses WHERE dataset_id = ? ORDER BY prompt_id, engine", (dataset_id,)).fetchall()
    citation_rows = conn.execute("SELECT * FROM citations WHERE dataset_id = ? ORDER BY response_id, order_no", (dataset_id,)).fetchall()
    prompts = {row["prompt_id"]: dict(row) for row in prompt_rows}
    responses = [dict(row) for row in response_rows]
    citations = {}
    for row in citation_rows:
        citations.setdefault(row["response_id"], []).append(dict(row))
    return {"dataset": dict(dataset), "prompts": prompts, "responses": responses, "citations": citations}


def create_analysis(conn, dataset_id, mode, score_version, category_version):
    analysis_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO analyses (analysis_id, dataset_id, status, mode, score_version, category_version, created_at) VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (analysis_id, dataset_id, mode, score_version, category_version, utc_now()),
    )
    conn.commit()
    return analysis_id


def start_analysis(conn, analysis_id):
    conn.execute("UPDATE analyses SET status = 'running', started_at = ? WHERE analysis_id = ?", (utc_now(), analysis_id))
    conn.commit()


def finish_analysis(conn, analysis_id, status, counts=None, error=None, report_id=None):
    counts = counts or {}
    conn.execute(
        """UPDATE analyses SET status = ?, finished_at = ?, total = ?, valid = ?, failed = ?, skipped = ?, error = ?, report_id = COALESCE(?, report_id)
           WHERE analysis_id = ?""",
        (
            status,
            utc_now(),
            counts.get("attempted", 0),
            counts.get("valid", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
            error,
            report_id,
            analysis_id,
        ),
    )
    conn.commit()


def save_result(conn, analysis_id, result):
    conn.execute(
        """INSERT INTO response_results (
            result_id, analysis_id, response_id, status, reason, mentioned, recommended, negative, position,
            competitors_json, evidence_json, labels_json, citation_count_raw, citation_count_deduped,
            brand_citation_count, mode, trace_json, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            analysis_id,
            result["response_id"],
            result["status"],
            result.get("reason"),
            1 if result["labels"]["mentioned"] else 0,
            1 if result["labels"]["recommended"] else 0,
            1 if result["labels"]["negative"] else 0,
            result.get("position"),
            json.dumps(result.get("competitors") or [], ensure_ascii=False),
            json.dumps(result.get("evidence") or [], ensure_ascii=False),
            json.dumps(result.get("labels") or {}, ensure_ascii=False),
            result.get("citations", {}).get("raw", 0),
            result.get("citations", {}).get("deduped", 0),
            result.get("citations", {}).get("brand", 0),
            result.get("mode", "replay"),
            json.dumps(result.get("trace") or {}, ensure_ascii=False),
            result.get("error"),
            utc_now(),
        ),
    )
    conn.commit()


def get_analysis(conn, analysis_id):
    return conn.execute("SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,)).fetchone()


def get_analysis_results(conn, analysis_id):
    rows = conn.execute(
        "SELECT * FROM response_results WHERE analysis_id = ? ORDER BY response_id", (analysis_id,)
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["competitors"] = json.loads(item.pop("competitors_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["labels"] = json.loads(item.pop("labels_json"))
        item["trace"] = json.loads(item.pop("trace_json"))
        results.append(item)
    return results


def create_report(conn, analysis_id, content, actor):
    report_id = str(uuid.uuid4())
    timestamp = utc_now()
    conn.execute(
        "INSERT INTO reports (report_id, analysis_id, current_version, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
        (report_id, analysis_id, timestamp, timestamp),
    )
    conn.execute(
        "INSERT INTO report_versions (report_id, version_number, status, content_json, created_by, created_at) VALUES (?, 1, 'draft', ?, ?, ?)",
        (report_id, json.dumps(content, ensure_ascii=False), actor, timestamp),
    )
    conn.execute("UPDATE analyses SET report_id = ? WHERE analysis_id = ?", (report_id, analysis_id))
    conn.commit()
    return report_id


def get_report(conn, report_id):
    return conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()


def get_report_version(conn, report_id, version_number):
    return conn.execute(
        "SELECT * FROM report_versions WHERE report_id = ? AND version_number = ?",
        (report_id, version_number),
    ).fetchone()


def list_report_versions(conn, report_id):
    rows = conn.execute(
        "SELECT version_number, status, created_by, created_at, submitted_by, submitted_at, decided_by, decided_at, decision_comment FROM report_versions WHERE report_id = ? ORDER BY version_number",
        (report_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def add_report_version(conn, report_id, content, actor):
    report = get_report(conn, report_id)
    version_number = report["current_version"] + 1
    timestamp = utc_now()
    conn.execute(
        "INSERT INTO report_versions (report_id, version_number, status, content_json, created_by, created_at) VALUES (?, ?, 'draft', ?, ?, ?)",
        (report_id, version_number, json.dumps(content, ensure_ascii=False), actor, timestamp),
    )
    conn.execute(
        "UPDATE reports SET current_version = ?, updated_at = ? WHERE report_id = ?",
        (version_number, timestamp, report_id),
    )
    conn.commit()
    return version_number


def update_draft_content(conn, report_id, version_number, content):
    timestamp = utc_now()
    conn.execute(
        "UPDATE report_versions SET content_json = ? WHERE report_id = ? AND version_number = ? AND status = 'draft'",
        (json.dumps(content, ensure_ascii=False), report_id, version_number),
    )
    conn.execute("UPDATE reports SET updated_at = ? WHERE report_id = ?", (timestamp, report_id))
    conn.commit()


def submit_version(conn, report_id, version_number, actor):
    timestamp = utc_now()
    conn.execute(
        "UPDATE report_versions SET status = 'submitted', submitted_by = ?, submitted_at = ? WHERE report_id = ? AND version_number = ?",
        (actor, timestamp, report_id, version_number),
    )
    conn.execute("UPDATE reports SET updated_at = ? WHERE report_id = ?", (timestamp, report_id))
    record_decision(conn, report_id, version_number, "submit", actor, None)
    conn.commit()


def decide_version(conn, report_id, version_number, decision, actor, comment):
    status = "approved" if decision == "approve" else "rejected"
    timestamp = utc_now()
    conn.execute(
        "UPDATE report_versions SET status = ?, decided_by = ?, decided_at = ?, decision_comment = ? WHERE report_id = ? AND version_number = ?",
        (status, actor, timestamp, comment, report_id, version_number),
    )
    conn.execute("UPDATE reports SET updated_at = ? WHERE report_id = ?", (timestamp, report_id))
    record_decision(conn, report_id, version_number, decision, actor, comment)
    conn.commit()


def record_decision(conn, report_id, version_number, action, actor_role, comment):
    conn.execute(
        "INSERT INTO review_decisions (decision_id, report_id, version_number, action, actor_role, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), report_id, version_number, action, actor_role, comment, utc_now()),
    )


def list_review_decisions(conn, report_id):
    rows = conn.execute(
        "SELECT version_number, action, actor_role, comment, created_at FROM review_decisions WHERE report_id = ? ORDER BY created_at",
        (report_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def report_summary(conn, report_id):
    report = get_report(conn, report_id)
    if report is None:
        return None
    current = get_report_version(conn, report_id, report["current_version"])
    content = json.loads(current["content_json"])
    return {
        "report_id": report_id,
        "analysis_id": report["analysis_id"],
        "current_version": report["current_version"],
        "status": current["status"],
        "created_at": report["created_at"],
        "updated_at": report["updated_at"],
        "content": content,
        "versions": list_report_versions(conn, report_id),
        "decisions": list_review_decisions(conn, report_id),
    }

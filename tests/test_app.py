import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import analyze
import app as app_module
import db
import llm

PACK = Path(__file__).resolve().parents[1]
ANALYST = {"Authorization": "Bearer analyst-test"}
REVIEWER = {"Authorization": "Bearer reviewer-test"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KASPARRO_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("KASPARRO_RESULTS", str(tmp_path / "results.json"))
    monkeypatch.setenv("KASPARRO_PACK_DIR", str(PACK))
    monkeypatch.setenv("ANALYST_TOKEN", "analyst-test")
    monkeypatch.setenv("REVIEWER_TOKEN", "reviewer-test")
    monkeypatch.setenv("KASPARRO_LLM_MODE", "replay")
    db.init_db()
    return TestClient(app_module.app)


def run_flow(client):
    response = client.post("/datasets", headers=ANALYST)
    assert response.status_code == 201
    dataset_id = response.json()["dataset_id"]
    response = client.post("/analyses", headers=ANALYST, json={"dataset_id": dataset_id})
    assert response.status_code == 201
    body = response.json()
    return body["analysis_id"], body["report_id"]


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def make_bad_pack(tmp_path, answer_text=None, name="badpack"):
    pack = tmp_path / name
    (pack / "data").mkdir(parents=True)
    (pack / "schemas").mkdir()
    shutil.copy(PACK / "schemas" / "input_contract.json", pack / "schemas" / "input_contract.json")
    run = {"run_id": "bad-run"}
    prompts = [{"prompt_id": "p1", "run_id": "bad-run", "text": "what is kasparro?"}]
    response = {
        "response_id": "r1",
        "run_id": "bad-run",
        "prompt_id": "p1",
        "engine": "chatgpt",
        "answer_text": answer_text,
        "capture_status": "captured",
        "archive_stored_at": "2026-07-31T00:00:00+00:00",
    }
    with open(pack / "data" / "run.json", "w", encoding="utf-8") as handle:
        json.dump(run, handle)
    write_jsonl(pack / "data" / "prompts.jsonl", prompts)
    write_jsonl(pack / "data" / "responses.jsonl", [response])
    write_jsonl(pack / "data" / "citations.jsonl", [])
    return pack


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert response.json()["execution_mode"] == "replay"


def test_invalid_input_is_rejected(client, tmp_path, monkeypatch):
    pack = make_bad_pack(tmp_path)
    monkeypatch.setenv("KASPARRO_PACK_DIR", str(pack))
    response = client.post("/datasets", headers=ANALYST)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "schema_validation_failed"


def test_duplicate_response_is_safely_reused(client):
    first = client.post("/datasets", headers=ANALYST)
    second = client.post("/datasets", headers=ANALYST)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["dataset_id"] == second.json()["dataset_id"]
    assert second.json()["reused_records"]["responses"] == 150
    assert second.json()["counts"]["responses"] == 150


def test_changed_duplicate_response_is_rejected(client, tmp_path, monkeypatch):
    first_pack = make_bad_pack(tmp_path, answer_text="Kasparro is a tool.", name="pack-one")
    monkeypatch.setenv("KASPARRO_PACK_DIR", str(first_pack))
    first = client.post("/datasets", headers=ANALYST)
    assert first.status_code == 201

    second_pack = make_bad_pack(tmp_path, answer_text="Kasparro is a different tool.", name="pack-two")
    monkeypatch.setenv("KASPARRO_PACK_DIR", str(second_pack))
    second = client.post("/datasets", headers=ANALYST)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_conflict"


def test_model_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setattr(llm, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("slow")))
    response = {"response_id": "r1", "engine": "chatgpt", "answer_text": "Kasparro is mentioned.", "answer_markdown": None}
    with pytest.raises(llm.ModelError) as error:
        llm.live_classify(response, {"text": "question"}, "live")
    assert error.value.code == "model_timeout"
    assert error.value.trace["attempts"] == 2


def test_malformed_model_output_is_rejected(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return b"this is not json"

    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm, "urlopen", lambda *args, **kwargs: FakeResponse())
    response = {"response_id": "r1", "engine": "chatgpt", "answer_text": "Kasparro is mentioned.", "answer_markdown": None}
    with pytest.raises(llm.ModelError) as error:
        llm.live_classify(response, {"text": "question"}, "live")
    assert error.value.code == "malformed_model_output"


def test_valid_live_model_output_is_parsed(monkeypatch):
    content = json.dumps({
        "mentioned": True,
        "recommended": True,
        "negative": False,
        "position": None,
        "competitors": ["Profound"],
        "evidence": [{"quote": "Kasparro is a good option.", "reason": "suitable option"}],
    })
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return body

    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm, "urlopen", lambda *args, **kwargs: FakeResponse())
    response = {"response_id": "r1", "engine": "chatgpt", "answer_text": "Kasparro is a good option.", "answer_markdown": None}
    result = llm.live_classify(response, {"text": "question"}, "live")
    assert result["labels"]["mentioned"] is True
    assert result["labels"]["recommended"] is True
    assert result["competitors"] == ["Profound"]
    assert result["trace"]["mode"] == "live"


def test_empty_model_output_is_rejected(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return b'{"choices":[{"message":{"content":""}}]}'

    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm, "urlopen", lambda *args, **kwargs: FakeResponse())
    response = {"response_id": "r1", "engine": "chatgpt", "answer_text": "Kasparro is mentioned.", "answer_markdown": None}
    with pytest.raises(llm.ModelError) as error:
        llm.live_classify(response, {"text": "question"}, "live")
    assert error.value.code == "empty_model_output"


def test_one_bad_record_does_not_break_the_analysis(client, monkeypatch):
    original = llm.classify

    def flaky(response, prompt, mode=None):
        if response["response_id"] == "ac5bdf24-6a6e-49a0-abf5-2755ff3b42bc":
            raise llm.ModelError("model_timeout", "controlled test timeout", {"mode": mode})
        return original(response, prompt, mode)

    monkeypatch.setattr(llm, "classify", flaky)
    analysis_id, report_id = run_flow(client)
    response = client.get("/analyses/" + analysis_id, headers=ANALYST)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["counts"]["failed"] == 1
    assert response.json()["counts"]["valid"] == 149
    assert report_id


def test_wrong_reviewer_role_is_denied(client):
    analysis_id, report_id = run_flow(client)
    submit = client.post("/reports/" + report_id + "/submit", headers=ANALYST)
    assert submit.status_code == 200
    denied = client.post(
        "/reports/" + report_id + "/decision",
        headers=ANALYST,
        json={"decision": "approve", "comment": "self approval"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden_role"


def test_rejection_blocks_export(client):
    analysis_id, report_id = run_flow(client)
    assert client.post("/reports/" + report_id + "/submit", headers=ANALYST).status_code == 200
    rejected = client.post(
        "/reports/" + report_id + "/decision",
        headers=REVIEWER,
        json={"decision": "reject", "comment": "evidence is incomplete"},
    )
    assert rejected.status_code == 200
    export = client.get("/reports/" + report_id + "/export", headers=ANALYST)
    assert export.status_code == 409
    assert export.json()["error"]["code"] == "export_not_approved"


def test_edit_after_approval_needs_new_review(client):
    analysis_id, report_id = run_flow(client)
    assert client.post("/reports/" + report_id + "/submit", headers=ANALYST).status_code == 200
    assert client.post(
        "/reports/" + report_id + "/decision",
        headers=REVIEWER,
        json={"decision": "approve", "comment": "approved for now"},
    ).status_code == 200
    assert client.get("/reports/" + report_id + "/export", headers=ANALYST).status_code == 200

    edited = client.patch("/reports/" + report_id, headers=ANALYST, json={"title": "Edited after approval"})
    assert edited.status_code == 200
    assert edited.json()["version_number"] == 2
    assert edited.json()["status"] == "draft"

    current_export = client.get("/reports/" + report_id + "/export", headers=ANALYST)
    assert current_export.status_code == 409
    old_export = client.get("/reports/" + report_id + "/export?version=1", headers=ANALYST)
    assert old_export.status_code == 200
    assert old_export.json()["version_number"] == 1
    assert old_export.json()["is_current"] is False


def test_score_arithmetic_is_deterministic():
    rows = [
        {"status": "ok", "branded": False, "mentioned": False, "recommended": False, "negative": False, "brand_citation": 0},
        {"status": "ok", "branded": False, "mentioned": True, "recommended": False, "negative": False, "brand_citation": 1},
        {"status": "ok", "branded": True, "mentioned": True, "recommended": True, "negative": False, "brand_citation": 1},
        {"status": "failed", "branded": True, "mentioned": False, "recommended": False, "negative": False, "brand_citation": 0},
    ]
    score = analyze.score_rows(rows)
    assert score["components"]["unprompted_discovery"]["value"] == 0.5
    assert score["components"]["branded_inclusion"]["value"] == 1.0
    assert score["components"]["recommendation"]["value"] == 1.0
    assert score["components"]["non_negative"]["value"] == 1.0
    assert score["components"]["brand_citation"]["value"] == 1.0
    assert score["score"] == 77.5
    assert score["failed"] == 1
    assert score["coverage"] == 0.75

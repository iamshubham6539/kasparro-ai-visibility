# Kasparro AI visibility backend

A small FastAPI + SQLite service that analyses the supplied 150 saved AI answers, scores Kasparro's visibility, produces three evidence-backed diagnostics and three remedies, and enforces human review before export. The report is written for a marketing or growth lead; the analyst and reviewer personas are represented by the two tokens.

The supplied data, reference, provenance, schema and fixture folders are treated as read-only inputs. The application stores its own database separately.

## What is in the box

```
app.py                  FastAPI routes, token auth, review state machine
db.py                   SQLite schema, ingestion and query helpers
analyze.py              classification, citation counting, scoring, aggregation
llm.py                  replay, stub and real-model adapters
streamlit_app.py        analyst and reviewer web console over the HTTP API
evaluate.py             evaluation runner; writes eval/report.md
eval/labels.json        14-case evaluation sample (8 tuning, 6 final, 2 edge cases)
eval/review_sample.md   human-review sheet with each selected prompt, answer and label
eval/report.md          generated evaluation report
tests/test_app.py       required engine and review tests
README.md
DECISION_NOTE.md
.env.example
requirements.txt
.gitignore
```

Runtime outputs are `kasparro.db` and `results.json`. The evaluator writes `eval/report.md` and `eval/review_sample.md`.

## Setup

Python 3.10 or later is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The application reads ordinary environment variables; it does not load `.env` by itself. For a local shell:

```bash
set -a
. ./.env
set +a
```

Initialise the database:

```bash
python3 -c "import db; db.init_db(); print('database ready')"
```

Start the API:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

FastAPI's generated docs are at `http://127.0.0.1:8000/docs` and `http://127.0.0.1:8000/openapi.json`.

The default development tokens are `analyst-dev-token` and `reviewer-dev-token`. Change them in `.env` before sharing the service.

Run commands from the project root. `KASPARRO_PACK_DIR` defaults to the project root and must contain the supplied `data/`, `schemas/` and `reference/` folders.

## Web frontend

`streamlit_app.py` is a basic analyst and reviewer console. It only calls the HTTP API; it never opens the database or imports the analysis code, so the token roles and the human-review gate still decide what can happen.

Start the API in one terminal:

```bash
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Start the frontend from the project root in another:

```bash
.venv/bin/streamlit run streamlit_app.py
```

Open the URL Streamlit prints, normally `http://localhost:8501`. The sidebar takes the API base URL and the two development tokens. It also remembers the report and analysis IDs created during the session; after a restart, paste a report ID from an earlier analysis response.

The console has four tabs:

1. **Import and analyse** validates and imports the supplied pack, then runs the replay analysis.
2. **Report** shows the current version, overall and grouped scores, coverage, execution details, the three diagnostics with their response IDs and excerpts, the three remedies, the 50-prompt comparison and the version history.
3. **Edit and submit** changes the title, summary and notes, optionally replaces the diagnostics or remedies JSON, saves a draft and submits it for review.
4. **Review and export** records an approve or reject decision with the reviewer token, then exports an approved version. Export fails with `409 export_not_approved` for any other status.

The frontend does not bypass the review gate. The analyst token cannot approve, and an edit made after approval creates a new draft that must be reviewed again.

## Commands

```bash
# tests
python3 -m pytest -q

# full replay analysis through the API (see curl below)
# or a quick out-of-process check
python3 -c "import db, analyze; conn=db.init_db(); print(db.import_pack('.', analyze.categorize_prompt))"

# evaluation, after revising eval/labels.json
python3 evaluate.py
```

`evaluate.py` reads `eval/labels.json`, runs the same cases through the simple rule baseline and the configured LLM approach, checks score arithmetic and diagnostic evidence, and writes `eval/report.md`. It reads the existing `results.json` for the generated-diagnostic evidence check, so run an analysis first.

Set `KASPARRO_ACTIVE_TIME` to your actual active work time before the final analysis. The value is copied into `results.json` as `active_time`. The committed run records `approximately 5 hours`.

## Execution modes

Replay is the default and needs no API key. It requires no model download, and the full 150-answer import and analysis completes in seconds:

```bash
KASPARRO_LLM_MODE=replay
```

Switch to a hosted OpenAI-compatible chat-completions endpoint:

```bash
KASPARRO_LLM_MODE=live
LLM_BASE_URL=https://api.openai.com/v1/chat/completions
LLM_MODEL=<model-name>
LLM_API_KEY=<key>
```

Switch to a local OpenAI-compatible endpoint. `local` does not require a key:

```bash
KASPARRO_LLM_MODE=local
LLM_BASE_URL=http://127.0.0.1:11434/v1/chat/completions
LLM_MODEL=<local-model-name>
```

`stub` is available for deterministic engineering tests. Bounds are controlled by `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_MAX_CALLS`, `LLM_MAX_INPUT_CHARS`, `LLM_MAX_OUTPUT_CHARS` and `LLM_MAX_TOKENS`.

Every response result and trace records its mode (`replay`, `stub`, `local` or `live`). The report also carries an `execution` block with the mode, configured model, classification call count and an explicit `cost_measurement: "not measured"` value. Replay demonstrates mechanics and failure handling only. It does not establish live-model correctness, variability, cost or latency.

## API

All protected routes use `Authorization: Bearer <token>`. The role is derived only from the token; no request body field can change it.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/health` | none | Database and execution-mode check |
| POST | `/datasets` | analyst | Validate and import the pack |
| POST | `/analyses` | analyst | Run the synchronous analysis |
| GET | `/analyses/{analysis_id}` | either | Status, counts and report ID |
| GET | `/reports/{report_id}` | either | Current report version and history |
| PATCH | `/reports/{report_id}` | analyst | Edit a draft, or create a new draft |
| POST | `/reports/{report_id}/submit` | analyst | Freeze the current version for review |
| POST | `/reports/{report_id}/decision` | reviewer | Approve or reject a submitted version |
| GET | `/reports/{report_id}/export` | either | Export an approved version |

Errors are structured:

```json
{"error": {"code": "forbidden_role", "message": "...", "details": {}}}
```

### Swagger UI

Open `http://127.0.0.1:8000/docs`. Click **Authorize**, paste the token **without** the `Bearer ` prefix, and click **Authorize** again. Swagger adds the prefix. Use `analyst-dev-token` for import, analysis and report edits; use `reviewer-dev-token` for the review decision. To switch roles, click **Authorize**, log out, and enter the other token. `/health` needs no token.

The OpenAPI schema declares one HTTP bearer scheme, so the same token applies to every protected route. You can still call the API with curl using the full `Authorization: Bearer <token>` header.

### Example flow

Set the two tokens for the shell:

```bash
ANALYST=analyst-dev-token
REVIEWER=reviewer-dev-token
BASE=http://127.0.0.1:8000
```

Health:

```bash
curl -s $BASE/health | python3 -m json.tool
```

Import the pack. The response includes `dataset_id`. Repeating the same import safely reuses identical records; a changed record with the same ID is rejected with `409`.

```bash
curl -s -X POST $BASE/datasets \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
```

Run the analysis. This is synchronous and bounded. It returns `analysis_id` and `report_id`.

```bash
curl -s -X POST $BASE/analyses \
  -H "Authorization: Bearer $ANALYST" \
  -H "Content-Type: application/json" \
  -d '{"dataset_id":"PASTE_DATASET_ID"}' | python3 -m json.tool
```

Check status:

```bash
curl -s $BASE/analyses/PASTE_ANALYSIS_ID \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
```

Read the draft report:

```bash
curl -s $BASE/reports/PASTE_REPORT_ID \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
```

Edit the draft. `title`, `summary` and `notes` are free text. If you send `diagnostics` or `remedies`, send all three objects and keep their required fields.

```bash
curl -s -X PATCH $BASE/reports/PASTE_REPORT_ID \
  -H "Authorization: Bearer $ANALYST" \
  -H "Content-Type: application/json" \
  -d '{"title":"Kasparro visibility review","summary":"Reviewed by the analyst."}' \
  | python3 -m json.tool
```

Submit. This freezes the current version:

```bash
curl -s -X POST $BASE/reports/PASTE_REPORT_ID/submit \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
```

Wrong-role denial. The analyst cannot approve their own report:

```bash
curl -s -X POST $BASE/reports/PASTE_REPORT_ID/decision \
  -H "Authorization: Bearer $ANALYST" \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"self approval"}' | python3 -m json.tool
# HTTP 403, code forbidden_role
```

Reviewer approval:

```bash
curl -s -X POST $BASE/reports/PASTE_REPORT_ID/decision \
  -H "Authorization: Bearer $REVIEWER" \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"Evidence links checked."}' | python3 -m json.tool
```

Export the approved current version:

```bash
curl -s $BASE/reports/PASTE_REPORT_ID/export \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
```

Rejection blocks export. A version can only be reviewed once, so use a fresh report (or a new draft version) that has been submitted:

```bash
# On a fresh submitted report, call it PASTE_REJECT_REPORT_ID
curl -s -X POST $BASE/reports/PASTE_REJECT_REPORT_ID/decision \
  -H "Authorization: Bearer $REVIEWER" \
  -H "Content-Type: application/json" \
  -d '{"decision":"reject","comment":"One diagnostic lacks a testable next step."}' \
  | python3 -m json.tool

curl -s $BASE/reports/PASTE_REJECT_REPORT_ID/export \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
# HTTP 409, code export_not_approved
```

Edit after approval. PATCH on an approved version creates a new draft and leaves the old approval attached to the old version:

```bash
curl -s -X PATCH $BASE/reports/PASTE_REPORT_ID \
  -H "Authorization: Bearer $ANALYST" \
  -H "Content-Type: application/json" \
  -d '{"title":"Changed after approval"}' | python3 -m json.tool

# current version is now a draft, so this is refused
curl -s $BASE/reports/PASTE_REPORT_ID/export \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
# HTTP 409

# the exact old approved version is still exportable and is clearly marked as not current
curl -s "$BASE/reports/PASTE_REPORT_ID/export?version=1" \
  -H "Authorization: Bearer $ANALYST" | python3 -m json.tool
```

## Ingestion rules

- Each prompt, response and citation record is validated against `schemas/input_contract.json` with `jsonschema` before anything is written.
- Raw JSON is preserved in the database alongside mapped columns.
- All inserts for one import happen in one transaction. A failed validation or conflict leaves no partial import.
- A repeated import of the same `run_id` reuses the existing dataset.
- For a repeated `response_id` (also prompt and citation IDs), identical raw content is safely reused. The same ID with different content returns `409 duplicate_conflict`; there is no silent duplicate.
- A duplicate ID inside one input file is rejected.
- Citations are checked against their response's prompt and engine.

## Analysis and scoring

The pack has 50 prompts and 150 captured answers, one answer per prompt per engine. 25 prompts name Kasparro (branded) and 25 do not (unbranded). The branded split is derived by the exact string match `\bkasparro\b` in the prompt.

Prompt categories are versioned as `categories_v1` and were inferred from the actual questions:

| Category | Prompt numbers |
|---|---|
| `efficacy_and_trust` | 1, 4, 5, 11, 12, 14, 15, 29, 30, 39, 40, 44 |
| `competitor_comparison` | 3, 8, 13, 17, 21, 24, 37, 38 |
| `capabilities_and_engines` | 2, 6, 7, 9, 10, 26, 27, 28, 31, 32, 33, 34, 35, 36, 42 |
| `pricing_and_contract` | 16, 18, 19, 22, 45, 46, 47 |
| `setup_and_onboarding` | 20, 23, 25, 41, 43, 48, 49, 50 |

The score is `score_v1`, on a 0-100 scale. It is a weighted sum of five rates:

| Component | Weight | Numerator | Denominator |
|---|---:|---|---|
| Unprompted discovery | 45% | valid unbranded responses mentioning Kasparro | valid unbranded responses |
| Branded inclusion | 20% | valid branded responses mentioning Kasparro | valid branded responses |
| Recommendation | 20% | valid branded responses explicitly recommending Kasparro | valid branded responses |
| Non-negative characterisation | 10% | mentioning responses without a negative label | mentioning responses |
| Brand citation presence | 5% | mentioning responses with at least one deduplicated `kasparro.com` citation | mentioning responses |

A component with a zero denominator is `null`, not zero. A composite score is produced only when all five denominators exist. Failed and skipped responses are excluded from denominators and always shown beside the score. The same grouped score function is used for the overall report, engines, categories and branded/unbranded splits.

Exact string matching is used for mention detection, citation counts, validation and permissions. The LLM is used only for judgment: recommendation versus passing mention, negative tone, competitors and position. The mention label is checked against the exact string match; an inconsistent model result becomes a failed result, not a confident answer.

Citations are deduplicated by `response_id` plus a normalised URL. Normalisation lowercases the scheme and host, removes a leading `www.`, strips URL fragments, removes common tracking parameters (`utm_*`, `ref`, `source`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`), removes a trailing slash and sorts the remaining query parameters. Raw occurrences remain in the database; the analysis reports raw, deduplicated and brand-citation counts.

`results.json` also records `active_time` (the candidate's actual active work time) and `execution.elapsed_seconds` (the replay run duration), alongside the execution mode, model, call count and cost measurement.

`answer_text` and `answer_markdown` are two views of one response. The classifier uses `answer_text` as its primary text and falls back to `answer_markdown` when `answer_text` is empty. Mention detection checks both views, and evidence must be an exact substring of one of them.

## Review state machine

A version is one of `draft`, `submitted`, `approved` or `rejected`.

- The analyst imports, analyses and edits a draft.
- Submit is allowed only for the current draft. It freezes that version's content.
- The reviewer approves or rejects only the current submitted version, with a comment.
- PATCH on a draft updates the same version. PATCH on a submitted, approved or rejected version creates a new draft version. The old version keeps its state and content.
- Export without a version exports only the current version, and only if it is approved. Draft, submitted and rejected versions are refused.
- Export with `?version=N` exports exactly that version if it is approved. An old approved version can therefore be exported explicitly after a newer draft exists, and the response marks `is_current: false`.
- Every submit, approve and reject writes an audit row with report ID, version, action, actor role, comment and time.

Tokens are compared with `secrets.compare_digest`. Request models forbid extra fields. Source text and model output never control identity or permissions.

## Evaluation labels

`eval/labels.json` is the labelling file. It contains a 14-case sample: eight tuning cases and six final cases, with two controlled edge cases marked `edge_case: true`. Four cases carry `expected_citations`, covering brand-citation present and absent. Replace or extend it with your own labels if you want the report to reflect your judgement. The committed sample was drafted with AI assistance and should be checked. `eval/review_sample.md` prints each selected prompt, answer and current label so the candidate can verify the sample before submission. Each case needs:

```json
{
  "response_id": "...",
  "labels": {"absent": false, "mentioned": true, "recommended": true, "negative": false},
  "note": "why this case was selected"
}
```

Optional fields include `position`, `competitors` and `expected_citations`. Add `diagnostic_checks` for evidence checks:

```json
{
  "case_id": "unsupported-example",
  "claim": "A deliberately unsupported claim",
  "response_ids": ["..."],
  "evidence_quotes": ["text that is not in the answer"],
  "expected_supported": false
}
```

The evaluator reports per-label precision and recall for a simple regex baseline and the configured LLM approach on the same cases, keeps tuning and final metrics separate, lists disagreements with the labels, checks citation-presence cases, checks that each remedy follows from its matching diagnostic, verifies score arithmetic independently, reports eligible/successful/failed/skipped counts and checks generated diagnostic excerpts against the saved answers. Replay results are marked as unverified model quality.

The committed run exits 0 with 14 eligible cases, 0 failures and 0 skips. The tuning and final splits are reported separately, and the report keeps the remaining classifier disagreements visible instead of hiding them.

## Data and security notes

- The database is separate from the supplied pack. The pack folders are not modified.
- `.gitignore` excludes the supplied data folders, generated outputs, local databases, virtual environments and caches from public Git. This private submission repository intentionally tracks the supplied inputs and generated outputs so reviewers can run it; do not make this repository public.
- Keep `ANALYST_TOKEN`, `REVIEWER_TOKEN` and any model key in environment variables. Never commit `.env`.
- The API is unauthenticated only for `/health` and the generated documentation.
- The synchronous analysis endpoint is intentionally simple. A real deployment would move long model runs to a queue, add per-user rate limits, add review expiry and protect against simultaneous reviewers.

## Known limits

- One historical capture per prompt and engine; no repeated-run reliability evidence.
- Recommendation and negative labels are model judgments. The labelled sample is small (14 cases, 2 edge cases) and was drafted with AI assistance.
- Citation presence is not source support; the pages were not all opened and inspected.
- Some model versions are null and remain unknown.
- The replay classifier is deterministic and demonstrates mechanics only.
- This prototype does not crawl the web, deploy, or promise ranking improvements.

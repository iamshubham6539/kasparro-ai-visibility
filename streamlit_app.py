"""Kasparro analyst and reviewer console.

Start the API first, then run this app from the project root:

    .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
    .venv/bin/streamlit run streamlit_app.py

The app only talks HTTP to the API. It never opens kasparro.db or imports
analyze.py, so token roles and the human-review gate stay in force.
"""

import json
import os

import httpx
import streamlit as st

DEFAULT_API_URL = os.getenv("KASPARRO_API_URL", "http://127.0.0.1:8000")
DEFAULT_ANALYST_TOKEN = os.getenv("ANALYST_TOKEN", "analyst-dev-token")
DEFAULT_REVIEWER_TOKEN = os.getenv("REVIEWER_TOKEN", "reviewer-dev-token")

REMEDY_FIELDS = (
    ("problem", "Problem"),
    ("evidence", "Evidence"),
    ("action", "Action"),
    ("benefit", "Expected benefit"),
    ("effort", "Effort"),
    ("uncertainty", "Uncertainty"),
    ("validation_step", "Validation step"),
)


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def call_api(base, method, path, token=None, body=None, timeout=180.0):
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token.strip()
    try:
        response = httpx.request(method, url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ApiError(0, "connection_error", "Could not reach " + url + ": " + str(exc)) from exc

    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:1000]}

    if response.status_code >= 400:
        error = data.get("error") if isinstance(data, dict) else None
        error = error or {}
        raise ApiError(
            response.status_code,
            error.get("code", "http_error"),
            error.get("message", "The API returned HTTP " + str(response.status_code)),
            error.get("details") or data,
        )
    return data


def show_api_error(exc):
    st.error(exc.message + " (" + exc.code + ")")
    if exc.status:
        st.caption("HTTP " + str(exc.status))
    if exc.details:
        with st.expander("Error details"):
            st.json(exc.details)


def flash(kind, message):
    st.session_state.flash = (kind, message)


def show_flash():
    item = st.session_state.pop("flash", None)
    if item:
        kind, message = item
        getattr(st, kind)(message)


def get_with_either_token(base, path, tokens, timeout=60):
    """Use whichever configured token the API accepts for a read-only route."""
    last_error = None
    seen = set()
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        try:
            return call_api(base, "GET", path, token=token, timeout=timeout)
        except ApiError as exc:
            last_error = exc
            if exc.status not in (401, 403):
                raise
    if last_error is not None:
        raise last_error
    raise ApiError(0, "missing_token", "Enter an analyst or reviewer token in the sidebar")


def read_report(base, report_id, tokens):
    return get_with_either_token(base, "/reports/" + report_id, tokens)


def status_note(status):
    if status == "draft":
        st.info("Draft: the analyst can edit it. It is not exportable until a reviewer approves it.")
    elif status == "submitted":
        st.warning("Submitted: waiting for a reviewer decision. The version is frozen for review.")
    elif status == "approved":
        st.success("Approved: this version can be exported. Any later edit creates a new draft and needs a new review.")
    elif status == "rejected":
        st.error("Rejected: this version cannot be exported. Edit the report to create a new draft and resubmit.")
    else:
        st.info("Status: " + str(status))


def fmt_value(value, digits=1):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return ("{:." + str(digits) + "f}").format(value)
    return str(value)


def fmt_rate(value):
    if value is None:
        return "n/a"
    return "{:.1%}".format(value)


def score_table(scores):
    rows = []
    for name, item in (scores or {}).items():
        rows.append(
            {
                "group": name,
                "score": item.get("score"),
                "valid": item.get("valid"),
                "attempted": item.get("attempted"),
                "failed": item.get("failed"),
                "coverage": fmt_rate(item.get("coverage")),
            }
        )
    return rows


def render_components(overall):
    weights = overall.get("weights") or {}
    rows = []
    for name, component in (overall.get("components") or {}).items():
        weight = weights.get(name, 0.0)
        value = component.get("value")
        rows.append(
            {
                "component": name.replace("_", " "),
                "weight": fmt_rate(weight),
                "numerator": component.get("numerator"),
                "denominator": component.get("denominator"),
                "rate": fmt_rate(value),
                "weighted points": None if value is None else round(value * weight * 100, 2),
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")


def render_diagnostics(content):
    diagnostics = content.get("diagnostics") or []
    if not diagnostics:
        st.info("No diagnostics in this report.")
        return
    for index, item in enumerate(diagnostics, start=1):
        title = (item.get("observation") or "Diagnostic").strip()
        with st.expander("Diagnostic " + str(index) + ": " + title[:110]):
            st.markdown("**Observation**")
            st.write(item.get("observation"))
            st.markdown("**Possible explanation**")
            st.write(item.get("possible_explanation"))
            st.markdown("**Test evidence**")
            st.write(item.get("test_evidence"))
            response_ids = item.get("response_ids") or []
            st.markdown("**Response IDs**")
            st.code("\n".join(response_ids) if response_ids else "none")
            excerpts = item.get("excerpts") or []
            if excerpts:
                st.markdown("**Quoted excerpts**")
                rows = []
                for excerpt in excerpts:
                    rows.append(
                        {
                            "response_id": excerpt.get("response_id"),
                            "prompt_id": excerpt.get("prompt_id"),
                            "engine": excerpt.get("engine"),
                            "quote": excerpt.get("quote"),
                        }
                    )
                st.dataframe(rows, hide_index=True, width="stretch")


def render_remedies(content):
    remedies = content.get("remedies") or []
    if not remedies:
        st.info("No remedies in this report.")
        return
    for index, item in enumerate(remedies, start=1):
        title = (item.get("problem") or "Remedy").strip()
        with st.expander("Remedy " + str(index) + ": " + title[:110]):
            for field, label in REMEDY_FIELDS:
                st.markdown("**" + label + "**")
                st.write(item.get(field) or "not provided")


def render_prompt_comparison(content):
    comparison = content.get("prompt_comparison") or []
    if not comparison:
        st.info("No prompt comparison in this report.")
        return

    categories = sorted({row.get("category", "") for row in comparison})
    left, right = st.columns([3, 2])
    with left:
        selected = st.multiselect("Categories", categories, default=categories)
    with right:
        prompt_type = st.radio("Prompt type", ["All", "Branded", "Unbranded"], horizontal=True)

    if not selected:
        st.info("Select at least one category.")
        return

    rows = []
    for item in comparison:
        if item.get("category") not in selected:
            continue
        if prompt_type == "Branded" and not item.get("branded"):
            continue
        if prompt_type == "Unbranded" and item.get("branded"):
            continue
        row = {
            "prompt": item.get("text"),
            "category": item.get("category"),
            "type": "branded" if item.get("branded") else "unbranded",
        }
        for engine, result in (item.get("engines") or {}).items():
            if result.get("status") != "ok":
                row[engine] = result.get("status")
            else:
                parts = ["mentioned" if result.get("mentioned") else "absent"]
                if result.get("recommended"):
                    parts.append("recommended")
                if result.get("negative"):
                    parts.append("negative")
                parts.append(str(result.get("brand_citations", 0)) + " brand cites")
                row[engine] = ", ".join(parts)
        rows.append(row)

    st.caption("Each engine cell shows mention state, recommendation, negative characterisation and brand citation count.")
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("Showing " + str(len(rows)) + " of " + str(len(comparison)) + " prompts.")


st.set_page_config(page_title="Kasparro review console", page_icon="K", layout="wide")

for pending, target in (
    ("pending_report_id", "report_id_input"),
    ("pending_analysis_id", "analysis_input"),
    ("pending_dataset_id", "dataset_input"),
):
    if pending in st.session_state:
        st.session_state[target] = st.session_state.pop(pending)

for key, default in (
    ("api_url", DEFAULT_API_URL),
    ("analyst_token", DEFAULT_ANALYST_TOKEN),
    ("reviewer_token", DEFAULT_REVIEWER_TOKEN),
    ("report_id_input", ""),
    ("analysis_input", ""),
    ("dataset_input", ""),
):
    st.session_state.setdefault(key, default)

with st.sidebar:
    st.header("Connection")
    base = st.text_input("API base URL", key="api_url").strip().rstrip("/")
    analyst_token = st.text_input("Analyst token", key="analyst_token", type="password")
    reviewer_token = st.text_input("Reviewer token", key="reviewer_token", type="password")
    st.caption("The API derives the role from the token. The app cannot change it.")

    health = None
    health_error = None
    try:
        health = call_api(base, "GET", "/health", timeout=10)
        st.success("API online")
        st.caption(
            "Mode: "
            + str(health.get("execution_mode"))
            + " · "
            + str((health.get("counts") or {}).get("reports", 0))
            + " reports · "
            + str((health.get("counts") or {}).get("datasets", 0))
            + " datasets"
        )
    except ApiError as exc:
        health_error = exc
        st.error("API offline")
        st.caption(exc.message)

    st.divider()
    st.header("Current records")
    report_id_input = st.text_input("Report ID", key="report_id_input", placeholder="paste a report ID")
    analysis_id_input = st.text_input("Analysis ID", key="analysis_input", placeholder="paste an analysis ID")
    st.caption("Run an analysis to fill these automatically, or paste an ID from an earlier analysis response.")

    st.divider()
    st.caption("Start the API with `.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000`.")

st.title("Kasparro review console")
st.caption("Import data, run the analysis, edit the draft, submit it for review, approve or reject it, and export only approved versions.")

if health is None:
    st.error("The API is not reachable at " + (base or "(no URL)") + ".")
    if health_error is not None:
        st.caption(health_error.message)
    st.markdown("Start the backend first:")
    st.code(
        ".venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000\n"
        ".venv/bin/streamlit run streamlit_app.py",
        language="bash",
    )
    st.stop()

show_flash()

report_id = st.session_state.report_id_input.strip()
report_data = None
report_error = None
if report_id:
    try:
        report_data = read_report(base, report_id, [analyst_token, reviewer_token])
    except ApiError as exc:
        report_error = exc

import_tab, report_tab, edit_tab, review_tab = st.tabs(
    ["1. Import and analyse", "2. Report", "3. Edit and submit", "4. Review and export"]
)

with import_tab:
    st.subheader("Import the supplied pack")
    st.write(
        "The API validates prompts, responses and citations against the supplied schema, then imports them in one transaction. "
        "Re-importing the same run reuses identical records."
    )
    if st.button("Import pack", type="primary"):
        with st.spinner("Validating and importing..."):
            try:
                result = call_api(base, "POST", "/datasets", token=analyst_token)
            except ApiError as exc:
                show_api_error(exc)
            else:
                st.session_state.import_result = result
                st.session_state.pending_dataset_id = result.get("dataset_id", "")
                flash("success", "Import complete. Dataset " + result.get("dataset_id", "") + " is ready.")
                st.rerun()

    import_result = st.session_state.get("import_result")
    if import_result:
        counts = import_result.get("counts") or {}
        columns = st.columns(4)
        columns[0].metric("Prompts", counts.get("prompts", 0))
        columns[1].metric("Responses", counts.get("responses", 0))
        columns[2].metric("Citations", counts.get("citations", 0))
        columns[3].metric("New import", "yes" if import_result.get("created") else "reused")
        st.caption("dataset_id: `" + str(import_result.get("dataset_id")) + "`")
    elif health.get("counts", {}).get("datasets"):
        st.caption("The database already has " + str(health["counts"]["datasets"]) + " dataset(s). Importing again is safe.")

    st.divider()
    st.subheader("Run the analysis")
    st.write("Replay mode is the default. The analysis is synchronous and needs no API key.")
    dataset_id = st.text_input(
        "Dataset ID (leave blank to use the latest)",
        key="dataset_input",
        placeholder="blank = latest imported dataset",
    )
    if st.button("Run analysis", type="primary"):
        body = {"dataset_id": dataset_id.strip()} if dataset_id.strip() else {}
        with st.spinner("Analysing 150 saved answers..."):
            try:
                result = call_api(base, "POST", "/analyses", token=analyst_token, body=body)
            except ApiError as exc:
                show_api_error(exc)
            else:
                st.session_state.analysis_result = result
                st.session_state.pending_report_id = result.get("report_id", "")
                st.session_state.pending_analysis_id = result.get("analysis_id", "")
                flash("success", "Analysis complete. The new report is loaded in the Report tab.")
                st.rerun()

    analysis_result = st.session_state.get("analysis_result")
    if analysis_result:
        counts = analysis_result.get("counts") or {}
        columns = st.columns(5)
        columns[0].metric("Valid", counts.get("valid", 0))
        columns[1].metric("Attempted", counts.get("attempted", 0))
        columns[2].metric("Failed", counts.get("failed", 0))
        columns[3].metric("Skipped", counts.get("skipped", 0))
        columns[4].metric("Mode", analysis_result.get("execution_mode", "n/a"))
        st.caption(
            "report_id: `"
            + str(analysis_result.get("report_id"))
            + "` · analysis_id: `"
            + str(analysis_result.get("analysis_id"))
            + "`"
        )

    st.divider()
    st.subheader("Check an analysis")
    if analysis_id_input.strip():
        if st.button("Check analysis status"):
            try:
                status = get_with_either_token(
                    base,
                    "/analyses/" + analysis_id_input.strip(),
                    [analyst_token, reviewer_token],
                )
            except ApiError as exc:
                show_api_error(exc)
            else:
                st.json(status)

with report_tab:
    if not report_id:
        st.info("Run an analysis or paste a report ID in the sidebar.")
    elif report_error is not None:
        show_api_error(report_error)
    elif report_data is not None:
        content = report_data.get("content") or {}
        overall = (content.get("scores") or {}).get("overall") or {}
        versions = report_data.get("versions") or []

        st.subheader(content.get("title") or "Untitled report")
        st.caption(
            "report `"
            + report_id
            + "` · current version "
            + str(report_data.get("current_version"))
            + " · analysis `"
            + str(report_data.get("analysis_id"))
            + "`"
        )
        status_note(report_data.get("status"))

        columns = st.columns(7)
        columns[0].metric("Overall score", fmt_value(overall.get("score")))
        columns[1].metric("Coverage", fmt_rate(overall.get("coverage")))
        columns[2].metric("Valid", str(overall.get("valid", 0)) + "/" + str(overall.get("attempted", 0)))
        columns[3].metric("Mentioned", overall.get("mentioned", 0))
        columns[4].metric("Recommended", overall.get("recommended", 0))
        columns[5].metric("Negative", overall.get("negative", 0))
        columns[6].metric("Failed", overall.get("failed", 0))

        summary_tab, scores_tab, findings_tab, prompts_tab, history_tab = st.tabs(
            ["Summary", "Scores", "Diagnostics and remedies", "Prompt comparison", "History"]
        )

        with summary_tab:
            st.markdown("**Analyst summary**")
            st.write(content.get("summary") or "No summary yet. Add one in the Edit and submit tab.")
            st.markdown("**Analyst notes**")
            st.write(content.get("notes") or "No notes.")
            st.markdown("**Execution**")
            execution = content.get("execution") or {}
            st.json(
                {
                    "mode": content.get("execution_mode") or execution.get("mode"),
                    "model": execution.get("model"),
                    "classification_calls": execution.get("classification_calls"),
                    "elapsed_seconds": execution.get("elapsed_seconds"),
                    "cost_measurement": execution.get("cost_measurement"),
                    "active_time": content.get("active_time"),
                }
            )
            st.markdown("**Coverage**")
            st.json(content.get("coverage") or {})
            st.markdown("**Versions used**")
            st.json(content.get("versions") or {})
            limitations = content.get("limitations") or []
            if limitations:
                st.markdown("**Limitations**")
                for limitation in limitations:
                    st.markdown("- " + str(limitation))

        with scores_tab:
            st.markdown("**Overall score components**")
            st.write(
                "The composite is withheld if any component has no eligible denominator. Failed and skipped responses stay visible but are excluded from denominators."
            )
            render_components(overall)

            if overall.get("missing_components"):
                st.warning("Missing components: " + ", ".join(overall["missing_components"]))

            st.markdown("**By engine**")
            st.dataframe(score_table((content.get("scores") or {}).get("by_engine")), hide_index=True, width="stretch")
            st.markdown("**By prompt category**")
            st.dataframe(score_table((content.get("scores") or {}).get("by_category")), hide_index=True, width="stretch")
            st.markdown("**Branded versus unbranded**")
            st.dataframe(score_table((content.get("scores") or {}).get("by_branded")), hide_index=True, width="stretch")

            with st.expander("By engine and category"):
                rows = []
                for engine, categories in ((content.get("scores") or {}).get("by_engine_category") or {}).items():
                    for category, item in categories.items():
                        rows.append(
                            {
                                "engine": engine,
                                "category": category,
                                "score": item.get("score"),
                                "valid": item.get("valid"),
                                "attempted": item.get("attempted"),
                                "coverage": fmt_rate(item.get("coverage")),
                            }
                        )
                st.dataframe(rows, hide_index=True, width="stretch")

        with findings_tab:
            st.markdown("## Diagnostics")
            st.caption("Each diagnostic links exact response IDs and quoted excerpts from the saved answers.")
            render_diagnostics(content)
            st.markdown("## Remedies")
            st.caption("Each remedy follows the diagnostic at the same position and includes a measurable validation step.")
            render_remedies(content)

        with prompts_tab:
            render_prompt_comparison(content)

        with history_tab:
            st.markdown("**Versions**")
            st.dataframe(versions, hide_index=True, width="stretch")
            st.markdown("**Review decisions**")
            decisions = report_data.get("decisions") or []
            if decisions:
                st.dataframe(decisions, hide_index=True, width="stretch")
            else:
                st.info("No reviewer decisions yet.")
            st.download_button(
                "Download current report JSON",
                data=json.dumps(report_data, indent=2),
                file_name="kasparro_report_" + report_id + ".json",
                mime="application/json",
            )

with edit_tab:
    if not report_id:
        st.info("Run an analysis or paste a report ID in the sidebar.")
    elif report_error is not None:
        show_api_error(report_error)
    elif report_data is not None:
        content = report_data.get("content") or {}
        st.subheader("Edit the current report")
        st.caption("Saving while the version is approved creates a new draft, so the old approval cannot be reused.")
        status_note(report_data.get("status"))

        with st.form("edit_report_form"):
            title = st.text_input("Title", value=content.get("title") or "", max_chars=200)
            summary = st.text_area("Summary", value=content.get("summary") or "", height=120, max_chars=5000)
            notes = st.text_area("Analyst notes", value=content.get("notes") or "", height=120, max_chars=5000)

            with st.expander("Edit diagnostics JSON (optional)"):
                send_diagnostics = st.checkbox("Send updated diagnostics")
                diagnostics_text = st.text_area(
                    "Diagnostics array",
                    value=json.dumps(content.get("diagnostics") or [], indent=2),
                    height=280,
                )

            with st.expander("Edit remedies JSON (optional)"):
                send_remedies = st.checkbox("Send updated remedies")
                remedies_text = st.text_area(
                    "Remedies array",
                    value=json.dumps(content.get("remedies") or [], indent=2),
                    height=280,
                )

            save = st.form_submit_button("Save draft", type="primary")

        if save:
            body = {}
            if title != (content.get("title") or ""):
                body["title"] = title
            if summary != (content.get("summary") or ""):
                body["summary"] = summary
            if notes != (content.get("notes") or ""):
                body["notes"] = notes

            parse_error = None
            if send_diagnostics:
                try:
                    parsed = json.loads(diagnostics_text)
                    if not isinstance(parsed, list):
                        raise ValueError("Diagnostics must be a JSON array.")
                    body["diagnostics"] = parsed
                except ValueError as exc:
                    parse_error = "Diagnostics: " + str(exc)
            if send_remedies:
                try:
                    parsed = json.loads(remedies_text)
                    if not isinstance(parsed, list):
                        raise ValueError("Remedies must be a JSON array.")
                    body["remedies"] = parsed
                except ValueError as exc:
                    parse_error = parse_error or ("Remedies: " + str(exc))

            if parse_error:
                st.error(parse_error)
            elif not body:
                st.info("No changes to save.")
            else:
                try:
                    result = call_api(base, "PATCH", "/reports/" + report_id, token=analyst_token, body=body)
                except ApiError as exc:
                    show_api_error(exc)
                else:
                    flash(
                        "success",
                        "Saved version " + str(result.get("version_number")) + " as a draft. It needs review before export.",
                    )
                    st.rerun()

        st.divider()
        st.subheader("Submit for review")
        if report_data.get("status") != "draft":
            st.info("Only a draft can be submitted. The current version is " + str(report_data.get("status")) + ".")
        else:
            st.write("Submitting freezes this version. The reviewer can then approve or reject it.")
            if st.button("Submit current version", type="primary"):
                try:
                    call_api(base, "POST", "/reports/" + report_id + "/submit", token=analyst_token)
                except ApiError as exc:
                    show_api_error(exc)
                else:
                    flash("success", "Report submitted for review.")
                    st.rerun()

with review_tab:
    if not report_id:
        st.info("Run an analysis or paste a report ID in the sidebar.")
    elif report_error is not None:
        show_api_error(report_error)
    elif report_data is not None:
        status = report_data.get("status")
        st.subheader("Review and export")
        st.caption("The reviewer token is required for decisions. The export route accepts either role but only returns an approved version.")
        status_note(status)

        decisions = report_data.get("decisions") or []
        if decisions:
            st.markdown("**Previous decisions**")
            st.dataframe(decisions, hide_index=True, width="stretch")

        st.divider()
        st.subheader("Reviewer decision")
        if status != "submitted":
            st.info("A decision needs a submitted version. The current version is " + str(status) + ".")
        else:
            with st.form("decision_form"):
                decision = st.radio("Decision", ["approve", "reject"], horizontal=True)
                comment = st.text_area("Comment (required)", height=100, max_chars=2000)
                decide = st.form_submit_button("Record decision", type="primary")
            if decide:
                if not comment.strip():
                    st.error("A comment is required.")
                else:
                    try:
                        call_api(
                            base,
                            "POST",
                            "/reports/" + report_id + "/decision",
                            token=reviewer_token,
                            body={"decision": decision, "comment": comment.strip()},
                        )
                    except ApiError as exc:
                        show_api_error(exc)
                    else:
                        flash("success", "Decision recorded: " + decision + ".")
                        st.rerun()

        st.divider()
        st.subheader("Export an approved version")
        st.write("Only approved versions can be exported. Exporting an older approved version is allowed; the response marks whether it is current.")
        approved_versions = [item.get("version_number") for item in (report_data.get("versions") or []) if item.get("status") == "approved"]
        current_version = int(report_data.get("current_version") or 1)
        if approved_versions:
            st.caption("Approved versions: " + ", ".join(str(item) for item in approved_versions))
        else:
            st.caption("No approved versions yet.")

        version = st.number_input("Version to export", min_value=1, max_value=current_version, value=current_version, step=1)
        if st.button("Export version", type="primary"):
            try:
                export = get_with_either_token(
                    base,
                    "/reports/" + report_id + "/export?version=" + str(int(version)),
                    [analyst_token, reviewer_token],
                )
            except ApiError as exc:
                show_api_error(exc)
            else:
                st.session_state.export_result = {
                    "report_id": report_id,
                    "version": int(version),
                    "data": export,
                }
                flash("success", "Exported approved version " + str(version) + ".")

        export_result = st.session_state.get("export_result")
        if export_result and export_result.get("report_id") == report_id:
            export = export_result["data"]
            st.success("Exported version " + str(export.get("version_number")) + ".")
            st.caption(
                "Current version: "
                + ("yes" if export.get("is_current") else "no (older approved version)")
                + " · exported at "
                + str(export.get("exported_at"))
            )
            st.download_button(
                "Download exported JSON",
                data=json.dumps(export, indent=2),
                file_name="kasparro_export_v" + str(export.get("version_number")) + ".json",
                mime="application/json",
            )
            with st.expander("Preview exported content"):
                export_content = export.get("content") or {}
                st.json(
                    {
                        "title": export_content.get("title"),
                        "summary": export_content.get("summary"),
                        "score": (export_content.get("scores") or {}).get("overall", {}).get("score"),
                        "diagnostics": len(export_content.get("diagnostics") or []),
                        "remedies": len(export_content.get("remedies") or []),
                    }
                )

import json
import os
import re
import sys
from pathlib import Path

import analyze
import llm

BASE = Path(__file__).resolve().parent
PACK_DIR = Path(os.getenv("KASPARRO_PACK_DIR", str(BASE)))
LABELS_PATH = Path(os.getenv("KASPARRO_LABELS", str(BASE / "eval" / "labels.json")))
REPORT_PATH = Path(os.getenv("KASPARRO_EVAL_REPORT", str(BASE / "eval" / "report.md")))
RESULTS_PATH = Path(os.getenv("KASPARRO_RESULTS", str(BASE / "results.json")))
LABELS = ("absent", "mentioned", "recommended", "negative")

REGRESSION = {
    "response_id": "ac5bdf24-6a6e-49a0-abf5-2755ff3b42bc",
    "expected": {"mentioned": True, "recommended": False, "negative": True},
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_pack():
    responses = {}
    prompts = {}
    with open(PACK_DIR / "data" / "responses.jsonl", "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                responses[record["response_id"]] = record
    with open(PACK_DIR / "data" / "prompts.jsonl", "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                prompts[record["prompt_id"]] = record
    return responses, prompts


def baseline_classify(response):
    text = response.get("answer_text") or response.get("answer_markdown") or ""
    mentioned = bool(re.search(r"\bkasparro\b", text, re.IGNORECASE))
    recommended = False
    negative = False
    if mentioned:
        for sentence in llm.split_sentences(text):
            if not re.search(r"\bkasparro\b", sentence, re.IGNORECASE):
                continue
            if re.search(r"\b(recommend|best|good option|strong option|worth|choose|pick|suitable)\b", sentence, re.IGNORECASE):
                if not re.search(r"\b(not|never|wouldn't|cannot|can't)\b", sentence, re.IGNORECASE):
                    recommended = True
            if re.search(r"\b(hype|gimmick|scam|overpriced|no evidence|not widely|risk|limited|weaker|inferior|skeptic)\b", sentence, re.IGNORECASE):
                negative = True
    return {
        "absent": not mentioned,
        "mentioned": mentioned,
        "recommended": recommended,
        "negative": negative,
    }


def llm_labels(response, prompt, mode):
    result = llm.classify(response, prompt, mode)
    return result["labels"], result


def collect_cases(labels_doc):
    cases = []
    seen = {}
    for split in ("tuning", "final"):
        for case in labels_doc.get(split, []):
            item = dict(case)
            item["split"] = split
            item["edge_case"] = bool(case.get("edge_case", False))
            cases.append(item)
            seen[case.get("response_id")] = item
    for case in labels_doc.get("edge_cases", []):
        response_id = case.get("response_id")
        if response_id in seen:
            seen[response_id]["edge_case"] = True
            continue
        item = dict(case)
        item["split"] = "final"
        item["edge_case"] = True
        cases.append(item)
        seen[response_id] = item
    return cases


def validate_case(case, responses):
    problems = []
    if not case.get("response_id"):
        problems.append("missing response_id")
    elif case["response_id"] not in responses:
        problems.append("response_id not found in data/responses.jsonl")
    expected = case.get("labels")
    if not isinstance(expected, dict):
        problems.append("missing labels object")
    else:
        for label in LABELS:
            if not isinstance(expected.get(label), bool):
                problems.append("labels." + label + " must be boolean")
    expected_citations = case.get("expected_citations")
    if expected_citations is not None:
        if not isinstance(expected_citations, dict):
            problems.append("expected_citations must be an object")
        elif isinstance(expected_citations.get("brand"), bool) or not isinstance(expected_citations.get("brand"), int) or expected_citations.get("brand") not in (0, 1):
            problems.append("expected_citations.brand must be 0 or 1")
    return problems


def metric(predictions, truths):
    tp = fp = fn = 0
    for predicted, truth in zip(predictions, truths):
        if predicted and truth:
            tp += 1
        elif predicted and not truth:
            fp += 1
        elif not predicted and truth:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = None
    if precision is not None and recall is not None and precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "support": tp + fn,
        "predicted": tp + fp,
    }


def metrics_for_cases(cases, predictions):
    output = {}
    for label in LABELS:
        truths = [case["labels"][label] for case in cases]
        predicted = [prediction["labels"][label] for prediction in predictions]
        output[label] = metric(predicted, truths)
    return output


def fmt(value):
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def metric_table(title, metrics):
    lines = [f"#### {title}", "", "| Label | Precision | Recall | F1 | Support | Predicted |", "|---|---:|---:|---:|---:|---:|"]
    for label in LABELS:
        item = metrics[label]
        lines.append(
            f"| {label} | {fmt(item['precision'])} | {fmt(item['recall'])} | {fmt(item['f1'])} | {item['support']} | {item['predicted']} |"
        )
    lines.append("")
    return "\n".join(lines)


def check_score_arithmetic():
    rows = [
        {"status": "ok", "branded": False, "mentioned": False, "recommended": False, "negative": False, "brand_citation": 0},
        {"status": "ok", "branded": False, "mentioned": True, "recommended": False, "negative": False, "brand_citation": 1},
        {"status": "ok", "branded": True, "mentioned": True, "recommended": True, "negative": False, "brand_citation": 1},
        {"status": "failed", "branded": True, "mentioned": False, "recommended": False, "negative": False, "brand_citation": 0},
    ]
    score = analyze.score_rows(rows)
    checks = [
        ("unprompted_discovery", score["components"]["unprompted_discovery"]["value"], 0.5),
        ("branded_inclusion", score["components"]["branded_inclusion"]["value"], 1.0),
        ("recommendation", score["components"]["recommendation"]["value"], 1.0),
        ("non_negative", score["components"]["non_negative"]["value"], 1.0),
        ("brand_citation", score["components"]["brand_citation"]["value"], 1.0),
        ("composite", score["score"], 77.5),
        ("failed visible", score["failed"], 1),
        ("coverage", score["coverage"], 0.75),
    ]
    return checks


def quote_in_response(quote, response):
    if not quote:
        return False
    text = response.get("answer_text") or ""
    markdown = response.get("answer_markdown") or ""
    return quote in text or quote in markdown


def check_diagnostic_claim(case, responses):
    response_ids = case.get("response_ids") or []
    quotes = case.get("evidence_quotes") or []
    ids_ok = bool(response_ids) and all(response_id in responses for response_id in response_ids)
    quotes_ok = bool(quotes)
    if ids_ok:
        for response_id in response_ids:
            for quote in quotes:
                if not quote_in_response(quote, responses[response_id]):
                    quotes_ok = False
    actual_supported = bool(ids_ok and quotes_ok)
    expected = case.get("expected_supported", True)
    return {
        "case_id": case.get("case_id", "unnamed"),
        "claim": case.get("claim", ""),
        "response_ids": response_ids,
        "expected_supported": expected,
        "actual_supported": actual_supported,
        "match": actual_supported == expected,
    }


def check_generated_diagnostics(results_doc, responses):
    if not results_doc:
        return {"available": False, "checked": 0, "supported": 0, "problems": []}
    checked = 0
    supported = 0
    problems = []
    for index, diagnostic in enumerate(results_doc.get("diagnostics", []), start=1):
        response_ids = diagnostic.get("response_ids") or []
        excerpts = diagnostic.get("excerpts") or []
        if not response_ids or not excerpts:
            problems.append("diagnostic " + str(index) + " has no IDs or excerpts")
            continue
        for excerpt in excerpts:
            checked += 1
            response_id = excerpt.get("response_id")
            quote = excerpt.get("quote", "")
            if response_id not in responses:
                problems.append("diagnostic " + str(index) + " has unknown response ID " + str(response_id))
                continue
            if not quote_in_response(quote, responses[response_id]):
                problems.append("diagnostic " + str(index) + " has an excerpt that is not in the answer")
                continue
            supported += 1
    return {"available": True, "checked": checked, "supported": supported, "problems": problems}


REMEDY_FIELDS = ("problem", "evidence", "action", "benefit", "effort", "uncertainty", "validation_step")


def check_citation_cases(cases, results_doc):
    if not results_doc:
        return {"available": False, "rows": []}
    by_id = {item.get("response_id"): item for item in results_doc.get("results", [])}
    rows = []
    for case in cases:
        expected = case.get("expected_citations")
        if not isinstance(expected, dict) or "brand" not in expected:
            continue
        item = by_id.get(case.get("response_id"))
        actual = None
        if item is not None:
            actual = 1 if (item.get("citations") or {}).get("brand", 0) > 0 else 0
        rows.append(
            {
                "case": case.get("response_id", "?"),
                "expected": expected["brand"],
                "actual": actual,
                "match": actual == expected["brand"] if actual is not None else False,
            }
        )
    return {"available": True, "rows": rows}


def check_remedies(results_doc):
    if not results_doc:
        return {"available": False, "rows": [], "passed": 0, "checked": 0}
    diagnostics = results_doc.get("diagnostics") or []
    remedies = results_doc.get("remedies") or []
    rows = []
    for index, remedy in enumerate(remedies):
        if not isinstance(remedy, dict):
            rows.append({"index": index + 1, "passed": False, "detail": "remedy is not an object"})
            continue
        missing = [field for field in REMEDY_FIELDS if not str(remedy.get(field, "")).strip()]
        response_ids = []
        if index < len(diagnostics) and isinstance(diagnostics[index], dict):
            response_ids = [str(item) for item in diagnostics[index].get("response_ids") or []]
        evidence = str(remedy.get("evidence", ""))
        ids_ok = bool(response_ids) and any(response_id in evidence for response_id in response_ids)
        validation = str(remedy.get("validation_step", ""))
        measurable = bool(re.search(r"\b(compare|rerun|baseline|measure|rate|count|increase|decrease)\b", validation, re.IGNORECASE))
        passed = not missing and ids_ok and measurable
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if not ids_ok:
            detail.append("evidence does not reference the matching diagnostic response IDs")
        if not measurable:
            detail.append("validation step is not measurable")
        rows.append(
            {
                "index": index + 1,
                "passed": passed,
                "detail": "; ".join(detail) or "fields, response IDs and validation step present",
            }
        )
    checked = len(rows)
    passed_count = sum(1 for row in rows if row["passed"])
    return {"available": True, "rows": rows, "passed": passed_count, "checked": checked}


def check_regression(responses):
    response = responses.get(REGRESSION["response_id"])
    if response is None:
        return {"available": False, "passed": False, "detail": "regression response not found"}
    result = llm.replay_classify(response)
    actual = {
        "mentioned": result["labels"]["mentioned"],
        "recommended": result["labels"]["recommended"],
        "negative": result["labels"]["negative"],
    }
    return {
        "available": True,
        "passed": actual == REGRESSION["expected"],
        "expected": REGRESSION["expected"],
        "actual": actual,
        "detail": "replay cue for conditional 'considering' wording was narrowed to explicit recommendation language",
    }


def write_report(lines):
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_review_sample(cases, responses, prompts):
    lines = [
        "# Evaluation sample review sheet",
        "",
        "Use this sheet to check every committed label yourself. Change `eval/labels.json` if you disagree, then run `python3 evaluate.py` again.",
        "",
        "For each case: confirm the prompt context, read the answer, and check absent / mentioned / recommended / negative plus any citation expectation.",
        "",
    ]
    split_counts = {"tuning": 0, "final": 0}
    for case in cases:
        split = case.get("split", "final")
        split_counts[split] = split_counts.get(split, 0) + 1
        if case.get("response_id") not in responses:
            continue
        response = responses[case["response_id"]]
        prompt = prompts.get(response["prompt_id"], {"text": ""})
        answer = response.get("answer_text") or response.get("answer_markdown") or ""
        lines.append("### " + split + " " + str(split_counts[split]) + ": `" + case["response_id"] + "`")
        lines.append("")
        lines.append("- Engine: `" + response["engine"] + "`")
        lines.append("- Prompt: " + prompt.get("text", ""))
        lines.append("- Current labels: `" + json.dumps(case["labels"], sort_keys=True) + "`")
        if "expected_citations" in case:
            lines.append("- Expected brand citation: `" + json.dumps(case["expected_citations"]) + "`")
        if case.get("edge_case"):
            lines.append("- Edge case: yes")
        lines.append("- Note: " + case.get("note", ""))
        lines.append("")
        lines.append("<details><summary>Answer</summary>")
        lines.append("")
        lines.append("```text")
        lines.append(answer)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    path = LABELS_PATH.parent / "review_sample.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    labels_doc = load_json(LABELS_PATH, default=None)
    responses, prompts = load_pack()
    mode = os.getenv("KASPARRO_LLM_MODE", "replay").strip().lower()
    lines = [
        "# Evaluation report",
        "",
        "Generated by `python3 evaluate.py`.",
        "",
        "Execution mode: **" + mode + "**",
        "",
    ]
    if mode in ("replay", "stub"):
        lines.extend(
            [
                "This run demonstrates the harness only. It does not establish live-model quality, variability, cost or latency.",
                "",
            ]
        )

    if labels_doc is None:
        lines.extend(
            [
                "## Labels missing",
                "",
                "No `eval/labels.json` file was found. Add at least 8 labelled supplied responses plus 2 edge cases.",
                "The file must contain `tuning` and `final` lists and may contain `diagnostic_checks`.",
                "",
            ]
        )
        write_report(lines)
        print("Labels file not found: " + str(LABELS_PATH))
        return 2

    if not isinstance(labels_doc, dict):
        lines.extend(["## Labels invalid", "", "The labels file must contain a JSON object.", ""])
        write_report(lines)
        print("Labels file is not a JSON object: " + str(LABELS_PATH))
        return 2

    cases = collect_cases(labels_doc)
    if not cases:
        lines.extend(
            [
                "## Labels pending",
                "",
                "`eval/labels.json` is present but contains no labelled cases. Add the tuning and final cases before running the comparison.",
                "",
            ]
        )
        write_report(lines)
        print("No labelled cases in " + str(LABELS_PATH))
        return 2

    eligible = len(cases)
    edge_cases = sum(1 for case in cases if case.get("edge_case"))
    successful = 0
    failed = 0
    skipped = 0
    baseline_predictions = []
    llm_predictions = []
    case_rows = []
    failures = []

    for case in cases:
        problems = validate_case(case, responses)
        if problems:
            skipped += 1
            failures.append({"case": case.get("response_id", "?"), "problem": "; ".join(problems)})
            case_rows.append((case, None, None, "; ".join(problems)))
            continue

        response = responses[case["response_id"]]
        prompt = prompts.get(response["prompt_id"], {"text": ""})
        baseline = baseline_classify(response)
        baseline_predictions.append({"case": case, "labels": baseline, "mode": "baseline"})

        try:
            predicted, raw = llm_labels(response, prompt, mode)
            successful += 1
            llm_predictions.append({"case": case, "labels": predicted, "mode": mode})
            case_rows.append((case, baseline, predicted, None))
        except llm.ModelError as exc:
            failed += 1
            failures.append({"case": case["response_id"], "problem": exc.code + ": " + exc.message})
            case_rows.append((case, baseline, None, exc.code))
        except Exception as exc:
            failed += 1
            failures.append({"case": case["response_id"], "problem": "internal_error: " + str(exc)})
            case_rows.append((case, baseline, None, "internal_error"))

    lines.extend(
        [
            "## Case counts",
            "",
            f"- Eligible cases: {eligible}",
            f"- Edge cases marked: {edge_cases}",
            f"- Successful LLM cases: {successful}",
            f"- Failed LLM cases: {failed}",
            f"- Skipped or invalid cases: {skipped}",
            "",
        ]
    )
    if failures:
        lines.append("Failures:")
        for failure in failures:
            lines.append("- `" + failure["case"] + "`: " + failure["problem"])
        lines.append("")

    baseline_cases = [item["case"] for item in baseline_predictions]
    llm_cases = [item["case"] for item in llm_predictions]
    baseline_metrics = metrics_for_cases(baseline_cases, baseline_predictions)
    llm_metrics = metrics_for_cases(llm_cases, llm_predictions)
    lines.append(metric_table("Rule baseline", baseline_metrics))
    lines.append(metric_table("LLM approach", llm_metrics))

    for split in ("tuning", "final"):
        split_baseline = [item for item in baseline_predictions if item["case"]["split"] == split]
        split_llm = [item for item in llm_predictions if item["case"]["split"] == split]
        if not split_baseline and not split_llm:
            continue
        lines.append("### Split: " + split)
        lines.append("")
        lines.append(metric_table("Rule baseline", metrics_for_cases([item["case"] for item in split_baseline], split_baseline)))
        lines.append(metric_table("LLM approach", metrics_for_cases([item["case"] for item in split_llm], split_llm)))

    lines.extend(["## Disagreements with the labels", "", "| Case | Label | Expected | Baseline | LLM |", "|---|---|---|---|---|"])
    disagreements = 0
    for case, baseline, llm_result, problem in case_rows:
        if baseline is None or llm_result is None:
            continue
        for label in LABELS:
            expected = bool(case["labels"][label])
            baseline_value = bool(baseline[label])
            llm_value = bool(llm_result[label])
            if baseline_value == expected and llm_value == expected:
                continue
            disagreements += 1
            lines.append(
                "| `"
                + case["response_id"][:12]
                + "` | "
                + label
                + " | "
                + str(expected)
                + " | "
                + str(baseline_value)
                + " | "
                + str(llm_value)
                + " |"
            )
    if disagreements == 0:
        lines.append("| none | - | - | - | - |")
    lines.append("")
    lines.append("A row appears when either approach differs from the expected label. The replay classifier is deterministic and is not evidence about a live model.")
    lines.append("")

    lines.extend(["## Case detail", "", "| Case | Split | Edge | Expected | Baseline | LLM | Note |", "|---|---|---|---|---|---|---|"])
    for case, baseline, llm_result, problem in case_rows:
        expected = case.get("labels", {})
        expected_text = ", ".join(label for label in LABELS if expected.get(label))
        baseline_text = ", ".join(label for label in LABELS if baseline and baseline[label]) if baseline else "not run"
        llm_text = ", ".join(label for label in LABELS if llm_result and llm_result[label]) if llm_result else "not run"
        note = case.get("note", "")
        if problem:
            note = (note + " " + problem).strip()
        lines.append(
            "| `"
            + str(case.get("response_id", "?"))[:12]
            + "` | "
            + case.get("split", "")
            + " | "
            + ("yes" if case.get("edge_case") else "no")
            + " | "
            + expected_text
            + " | "
            + baseline_text
            + " | "
            + llm_text
            + " | "
            + note.replace("|", "/")
            + " |"
        )
    lines.append("")

    score_checks = check_score_arithmetic()
    lines.extend(["## Score arithmetic check", "", "| Check | Actual | Expected | Result |", "|---|---:|---:|---|"])
    for name, actual, expected in score_checks:
        result = "PASS" if actual == expected else "FAIL"
        lines.append(f"| {name} | {actual} | {expected} | {result} |")
    lines.append("")

    lines.extend(["## Diagnostic evidence checks", ""])
    diagnostic_checks = labels_doc.get("diagnostic_checks") or []
    if diagnostic_checks:
        lines.extend(["| Case | Expected | Actual | Match | Claim |", "|---|---|---|---|---|"])
        for case in diagnostic_checks:
            checked = check_diagnostic_claim(case, responses)
            lines.append(
                "| "
                + checked["case_id"]
                + " | "
                + str(checked["expected_supported"])
                + " | "
                + str(checked["actual_supported"])
                + " | "
                + ("PASS" if checked["match"] else "FAIL")
                + " | "
                + checked["claim"].replace("|", "/")
                + " |"
            )
        lines.append("")
    else:
        lines.extend(["No `diagnostic_checks` were supplied in the labels file.", ""])

    results_doc = load_json(RESULTS_PATH, default=None)
    generated = check_generated_diagnostics(results_doc, responses)
    if generated["available"]:
        lines.extend(
            [
                "Generated diagnostics in `results.json`: "
                + str(generated["supported"])
                + "/"
                + str(generated["checked"])
                + " excerpts matched the saved answers.",
                "",
            ]
        )
        if generated["problems"]:
            lines.append("Problems:")
            for problem in generated["problems"]:
                lines.append("- " + problem)
            lines.append("")
    else:
        lines.extend(["`results.json` was not found, so generated diagnostic evidence was not checked.", ""])

    citation_checks = check_citation_cases(cases, results_doc)
    lines.extend(["## Citation presence checks", ""])
    if citation_checks["available"] and citation_checks["rows"]:
        lines.extend(["| Case | Expected brand citation | Actual brand citation | Match |", "|---|---:|---:|---|"])
        for row in citation_checks["rows"]:
            lines.append(
                "| `"
                + str(row["case"])[:12]
                + "` | "
                + str(row["expected"])
                + " | "
                + ("missing" if row["actual"] is None else str(row["actual"]))
                + " | "
                + ("PASS" if row["match"] else "FAIL")
                + " |"
            )
        lines.append("")
        lines.append("Brand citation means at least one deduplicated `kasparro.com` citation for that response. A citation is not treated as proof that the source supports a claim.")
        lines.append("")
    else:
        lines.extend(["No cases declared `expected_citations` in the labels file, or `results.json` was not available.", ""])

    remedy_checks = check_remedies(results_doc)
    lines.extend(["## Remedy traceability checks", ""])
    if remedy_checks["available"] and remedy_checks["rows"]:
        lines.extend(["| Remedy | Result | Detail |", "|---:|---|---|"])
        for row in remedy_checks["rows"]:
            lines.append(
                "| "
                + str(row["index"])
                + " | "
                + ("PASS" if row["passed"] else "FAIL")
                + " | "
                + row["detail"].replace("|", "/")
                + " |"
            )
        lines.append("")
        lines.append(
            "Each remedy must contain all required fields, reference the matching diagnostic's response IDs in its evidence, and include a measurable validation step. This checks traceability, not whether the proposed action will work."
        )
        lines.append("")
    else:
        lines.extend(["`results.json` was not available, so remedies were not checked.", ""])

    regression = check_regression(responses)
    lines.extend(["## Failure, fix and regression", ""])
    if regression["available"]:
        lines.append(
            "- Failure: replay marked the phrase \"If you're considering Kasparro specifically...\" as a recommendation because the cue was too broad."
        )
        lines.append(
            "- Diagnosis: mentioning a conditional consideration does not mean the answer presents Kasparro as suitable."
        )
        lines.append(
            "- Change: the recommendation cue now requires explicit recommendation or suitability language."
        )
        lines.append(
            "- Regression case `"
            + REGRESSION["response_id"]
            + "`: "
            + ("PASS" if regression["passed"] else "FAIL")
            + " (expected "
            + str(regression["expected"])
            + ", actual "
            + str(regression["actual"])
            + ")."
        )
    else:
        lines.append("- Regression response was not available in this pack.")
    lines.append("")

    if results_doc:
        coverage = results_doc.get("coverage", {})
        lines.extend(
            [
                "## End-to-end analysis coverage",
                "",
                f"- Attempted: {coverage.get('attempted', 'n/a')}",
                f"- Valid: {coverage.get('valid', 'n/a')}",
                f"- Failed: {coverage.get('failed', 'n/a')}",
                f"- Skipped: {coverage.get('skipped', 'n/a')}",
                f"- Coverage: {coverage.get('coverage', 'n/a')}",
                "",
            ]
        )

    lines.extend(
        [
            "## Grader limits",
            "",
            "- Labels are a small labelled sample, not a population estimate.",
            "- The committed sample labels were drafted with AI assistance and should be independently checked before submission.",
            "- The final split was used once for reporting and was not used to change the classifier in this revision.",
            "- The rule baseline and replay approach are deterministic; a live model can vary between runs.",
            "- Exact-substring checks confirm that a quote exists, not that it supports the wider claim.",
            "- Diagnostic prose is generated from counts and templates; an LLM judging its own prose would not be sufficient evidence.",
            "- A single capture per prompt and engine cannot establish persistent engine behaviour.",
            "",
        ]
    )

    write_report(lines)
    review_path = write_review_sample(cases, responses, prompts)
    print("Wrote " + str(REPORT_PATH))
    print("Wrote " + str(review_path))
    print("Cases: eligible=" + str(eligible) + " successful=" + str(successful) + " failed=" + str(failed) + " skipped=" + str(skipped))
    return 0 if failed == 0 and skipped == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

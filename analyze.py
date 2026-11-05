import json
import os
import re
import time
from collections import defaultdict

import db
import llm

CATEGORY_VERSION = "categories_v1"
SCORE_VERSION = "score_v1"
WEIGHTS = {
    "unprompted_discovery": 0.45,
    "branded_inclusion": 0.20,
    "recommendation": 0.20,
    "non_negative": 0.10,
    "brand_citation": 0.05,
}

CATEGORY_PROMPTS = {
    "efficacy_and_trust": [1, 4, 5, 11, 12, 14, 15, 29, 30, 39, 40, 44],
    "competitor_comparison": [3, 8, 13, 17, 21, 24, 37, 38],
    "capabilities_and_engines": [2, 6, 7, 9, 10, 26, 27, 28, 31, 32, 33, 34, 35, 36, 42],
    "pricing_and_contract": [16, 18, 19, 22, 45, 46, 47],
    "setup_and_onboarding": [20, 23, 25, 41, 43, 48, 49, 50],
}

PROMPT_CATEGORIES = {}
for category_name, numbers in CATEGORY_PROMPTS.items():
    for number in numbers:
        PROMPT_CATEGORIES[number] = category_name


def prompt_number(prompt_id):
    match = re.search(r"(\d+)$", prompt_id)
    if match is None:
        return None
    return int(match.group(1))


def categorize_prompt(prompt_id, text):
    number = prompt_number(prompt_id)
    category = PROMPT_CATEGORIES.get(number)
    if category is None:
        low = text.lower()
        if any(word in low for word in ("price", "cost", "pricing", "retainer", "cancel", "expensive")):
            category = "pricing_and_contract"
        elif any(word in low for word in ("setup", "onboard", "integration", "sign up", "self serve", "sales")):
            category = "setup_and_onboarding"
        elif any(word in low for word in ("compare", "versus", " vs ", "better than", "alternative", "prefer", "trustworthy")):
            category = "competitor_comparison"
        elif any(word in low for word in ("recommend", "track", "measure", "visibility", "engine", "audit", "feature")):
            category = "capabilities_and_engines"
        else:
            category = "efficacy_and_trust"
    return {
        "category": category,
        "branded": llm.has_mention(text),
        "version": CATEGORY_VERSION,
    }


def citation_summary(rows):
    raw = len(rows)
    seen = set()
    brand = 0
    for row in rows:
        key = row.get("url_normalized") or db.normalise_url(row.get("url", ""))
        if key in seen:
            continue
        seen.add(key)
        host = db.url_host(row.get("url", ""))
        if host == "kasparro.com" or host.endswith(".kasparro.com"):
            brand += 1
    return {"raw": raw, "deduped": len(seen), "brand": brand}


def answer_texts(response):
    text = response.get("answer_text") or ""
    markdown = response.get("answer_markdown") or ""
    return text, markdown


def primary_answer(response):
    text, markdown = answer_texts(response)
    if text:
        return text, "answer_text"
    return markdown, "answer_markdown"


def exact_quote(quote, text, markdown):
    if not quote:
        return False
    return quote in text or quote in markdown


def mention_quote(response):
    text, field = primary_answer(response)
    for sentence in llm.split_sentences(text):
        if llm.has_mention(sentence):
            return {"field": field, "quote": sentence, "reason": "exact brand-name match"}
    match = llm.MENTION_RE.search(text)
    if match:
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        return {"field": field, "quote": text[start:end], "reason": "exact brand-name match"}
    return None


def first_sentence(response):
    text, field = primary_answer(response)
    sentences = llm.split_sentences(text)
    if sentences:
        return {"field": field, "quote": sentences[0], "reason": "answer does not name Kasparro"}
    return None


def classify_one(response, prompt, mode):
    text, markdown = answer_texts(response)
    if not text.strip() and not markdown.strip():
        return {
            "response_id": response["response_id"],
            "prompt_id": response["prompt_id"],
            "engine": response["engine"],
            "status": "skipped",
            "reason": "empty_answer",
            "labels": {"absent": False, "mentioned": False, "recommended": False, "negative": False},
            "position": None,
            "competitors": [],
            "evidence": [],
            "citations": citation_summary([]),
            "mode": mode,
            "trace": {"mode": mode, "reason": "empty_answer"},
            "error": None,
        }

    exact_mention = llm.has_mention(text) or llm.has_mention(markdown)
    try:
        model_result = llm.classify(response, prompt, mode)
        labels = model_result["labels"]
        if labels["mentioned"] != exact_mention:
            if exact_mention and not labels["mentioned"]:
                labels["mentioned"] = True
                labels["absent"] = False
            else:
                raise llm.ModelError("inconsistent_model_output", "Model mentioned label disagrees with exact string match")

        if not exact_mention and (labels["recommended"] or labels["negative"]):
            raise llm.ModelError("inconsistent_model_output", "Model recommended or negative label without a brand mention")

        evidence = []
        for item in model_result.get("evidence", []):
            quote = item.get("quote", "")
            if exact_quote(quote, text, markdown):
                field = "answer_text" if quote in text else "answer_markdown"
                evidence.append({"field": field, "quote": quote, "reason": item.get("reason", "")[:200]})

        if exact_mention and not evidence:
            quote = mention_quote(response)
            if quote:
                evidence.append(quote)
        if not exact_mention and not evidence:
            quote = first_sentence(response)
            if quote:
                evidence.append(quote)

        if labels["recommended"] and not evidence:
            raise llm.ModelError("unsupported_evidence", "Recommended label had no exact answer evidence")
        if labels["negative"] and not evidence:
            raise llm.ModelError("unsupported_evidence", "Negative label had no exact answer evidence")

        position = model_result.get("position")
        if not labels["recommended"] or position is not None and (position < 1 or position > 50):
            position = None

        competitors = []
        for name in model_result.get("competitors", []):
            name = " ".join(str(name).split())
            if name and name not in competitors and len(name) <= 80:
                competitors.append(name)
        competitors = competitors[:10]

        return {
            "response_id": response["response_id"],
            "prompt_id": response["prompt_id"],
            "engine": response["engine"],
            "status": "ok",
            "reason": None,
            "labels": labels,
            "position": position,
            "competitors": competitors,
            "evidence": evidence[:5],
            "citations": citation_summary(response.get("citations", [])),
            "mode": model_result.get("mode", mode),
            "trace": model_result.get("trace", {"mode": mode}),
            "error": None,
        }
    except llm.ModelError as exc:
        return {
            "response_id": response["response_id"],
            "prompt_id": response["prompt_id"],
            "engine": response["engine"],
            "status": "failed",
            "reason": exc.code,
            "labels": {"absent": False, "mentioned": False, "recommended": False, "negative": False},
            "position": None,
            "competitors": [],
            "evidence": [],
            "citations": citation_summary(response.get("citations", [])),
            "mode": mode,
            "trace": exc.trace or {"mode": mode},
            "error": exc.message,
        }
    except Exception as exc:
        return {
            "response_id": response["response_id"],
            "prompt_id": response["prompt_id"],
            "engine": response["engine"],
            "status": "failed",
            "reason": "internal_error",
            "labels": {"absent": False, "mentioned": False, "recommended": False, "negative": False},
            "position": None,
            "competitors": [],
            "evidence": [],
            "citations": citation_summary(response.get("citations", [])),
            "mode": mode,
            "trace": {"mode": mode},
            "error": str(exc),
        }


def rate(numerator, denominator):
    if denominator == 0:
        return {"numerator": numerator, "denominator": denominator, "value": None}
    return {"numerator": numerator, "denominator": denominator, "value": round(numerator / denominator, 4)}


def score_rows(rows):
    attempted = len(rows)
    valid = [row for row in rows if row["status"] == "ok"]
    failed = [row for row in rows if row["status"] == "failed"]
    skipped = [row for row in rows if row["status"] == "skipped"]

    unbranded = [row for row in valid if not row["branded"]]
    branded = [row for row in valid if row["branded"]]
    mentioned = [row for row in valid if row["mentioned"]]

    components = {
        "unprompted_discovery": rate(sum(1 for row in unbranded if row["mentioned"]), len(unbranded)),
        "branded_inclusion": rate(sum(1 for row in branded if row["mentioned"]), len(branded)),
        "recommendation": rate(sum(1 for row in branded if row["recommended"]), len(branded)),
        "non_negative": rate(sum(1 for row in mentioned if not row["negative"]), len(mentioned)),
        "brand_citation": rate(sum(1 for row in mentioned if row["brand_citation"] > 0), len(mentioned)),
    }

    missing = [name for name, component in components.items() if component["value"] is None]
    if missing:
        score = None
    else:
        total = 0.0
        for name, weight in WEIGHTS.items():
            total += weight * components[name]["value"]
        score = round(total * 100, 1)

    return {
        "attempted": attempted,
        "valid": len(valid),
        "failed": len(failed),
        "skipped": len(skipped),
        "coverage": round(len(valid) / attempted, 4) if attempted else None,
        "score": score,
        "missing_components": missing,
        "components": components,
        "weights": WEIGHTS,
        "mentioned": len(mentioned),
        "recommended": sum(1 for row in valid if row["recommended"]),
        "negative": sum(1 for row in valid if row["negative"]),
    }


def make_rows(results, prompts):
    rows = []
    for result in results:
        prompt = prompts[result["prompt_id"]]
        rows.append(
            {
                "response_id": result["response_id"],
                "prompt_id": result["prompt_id"],
                "engine": result["engine"],
                "category": prompt["category"],
                "branded": bool(prompt["branded"]),
                "status": result["status"],
                "mentioned": bool(result["labels"]["mentioned"]) if result["status"] == "ok" else False,
                "recommended": bool(result["labels"]["recommended"]) if result["status"] == "ok" else False,
                "negative": bool(result["labels"]["negative"]) if result["status"] == "ok" else False,
                "brand_citation": result.get("citations", {}).get("brand", 0),
            }
        )
    return rows


def grouped_scores(rows, key_function):
    groups = defaultdict(list)
    for row in rows:
        groups[key_function(row)].append(row)
    output = {}
    for name in sorted(groups):
        output[name] = score_rows(groups[name])
    return output


def engine_category_scores(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["engine"], row["category"])].append(row)
    output = {}
    for engine, category in sorted(groups):
        output.setdefault(engine, {})[category] = score_rows(groups[(engine, category)])
    return output


def prompt_comparison(prompts, results):
    by_prompt = defaultdict(dict)
    for result in results:
        by_prompt[result["prompt_id"]][result["engine"]] = {
            "response_id": result["response_id"],
            "status": result["status"],
            "reason": result.get("reason"),
            "mentioned": bool(result["labels"]["mentioned"]) if result["status"] == "ok" else None,
            "recommended": bool(result["labels"]["recommended"]) if result["status"] == "ok" else None,
            "negative": bool(result["labels"]["negative"]) if result["status"] == "ok" else None,
            "position": result.get("position"),
            "brand_citations": result.get("citations", {}).get("brand", 0),
        }
    output = []
    for prompt_id in sorted(prompts):
        prompt = prompts[prompt_id]
        output.append(
            {
                "prompt_id": prompt_id,
                "text": prompt["text"],
                "category": prompt["category"],
                "branded": bool(prompt["branded"]),
                "engines": by_prompt.get(prompt_id, {}),
            }
        )
    return output


def evidence_items(results, predicate, limit=3, reason_contains=None):
    items = []
    seen = set()
    for result in results:
        if not predicate(result):
            continue
        for evidence in result.get("evidence", []):
            quote = (evidence.get("quote") or "").strip()
            if not quote:
                continue
            if reason_contains and reason_contains not in evidence.get("reason", ""):
                continue
            key = (result["response_id"], quote)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "response_id": result["response_id"],
                    "prompt_id": result["prompt_id"],
                    "engine": result["engine"],
                    "quote": quote,
                    "reason": evidence.get("reason", ""),
                }
            )
            break
        if len(items) >= limit:
            break
    return items


def response_ids_from_evidence(evidence):
    ids = []
    for item in evidence:
        if item["response_id"] not in ids:
            ids.append(item["response_id"])
    return ids


def build_diagnostics(results, prompts, scores):
    overall = scores["overall"]
    discovery = overall["components"]["unprompted_discovery"]
    inclusion = overall["components"]["branded_inclusion"]
    negative_component = overall["components"]["non_negative"]

    absent_unbranded = [
        result for result in results if result["status"] == "ok" and not prompts[result["prompt_id"]]["branded"] and not result["labels"]["mentioned"]
    ]
    absent_unbranded.sort(key=lambda result: (len(result.get("competitors") or []) == 0, result["response_id"]))
    absent_evidence = evidence_items(absent_unbranded, lambda item: True, 3)

    diagnostics = []
    diagnostics.append(
        {
            "observation": (
                f"Kasparro was named in {discovery['numerator']} of {discovery['denominator']} valid unbranded answers, "
                f"but in {inclusion['numerator']} of {inclusion['denominator']} valid branded answers."
            ),
            "possible_explanation": (
                "The snapshot is consistent with Kasparro being recognised when the question supplies its name, "
                "but not being associated with the underlying buyer questions. The capture cannot show why; "
                "source selection, content coverage and engine behaviour could all contribute."
            ),
            "test_evidence": (
                "Inspect the sources cited by the absent unbranded answers, compare them with Kasparro's own pages, "
                "and rerun the same 25 unbranded prompts after any change. A repeatable rise in unprompted mentions "
                "would be more informative than one changed answer."
            ),
            "response_ids": response_ids_from_evidence(absent_evidence),
            "excerpts": absent_evidence,
        }
    )

    negative_results = [
        result for result in results if result["status"] == "ok" and result["labels"]["negative"]
    ]
    negative_evidence = evidence_items(negative_results, lambda item: True, 3, reason_contains="negative")
    if negative_results:
        observation = (
            f"{len(negative_results)} of {overall['mentioned']} valid mentioning answers carried a negative label; "
            f"{negative_component['numerator']} of {negative_component['denominator']} mentioning answers were non-negative."
        )
        explanation = (
            "Several answers question Kasparro's independent evidence, maturity or value. That is a recorded wording "
            "pattern, not proof of the underlying business condition."
        )
        test = (
            "Inspect the cited pages behind these answers and collect dated, verifiable proof that addresses the specific "
            "doubt. Rerun the same branded prompts and compare the negative-label rate and the cited sources."
        )
    else:
        observation = (
            f"Only {overall['components']['recommendation']['numerator']} of "
            f"{overall['components']['recommendation']['denominator']} valid branded answers explicitly recommended Kasparro, "
            "and no negative labels were recorded."
        )
        explanation = (
            "The recorded answers mostly describe Kasparro without presenting it as a suitable option. The snapshot cannot "
            "separate missing recommendation cues from a genuine lack of endorsement."
        )
        test = (
            "Review the branded answers that mention but do not recommend Kasparro, then test whether clearer comparison "
            "and proof content changes the recommendation rate on the same prompts."
        )
        negative_evidence = evidence_items(
            [result for result in results if result["status"] == "ok" and result["labels"]["mentioned"] and not result["labels"]["recommended"]],
            lambda item: True,
            3,
        )
    diagnostics.append(
        {
            "observation": observation,
            "possible_explanation": explanation,
            "test_evidence": test,
            "response_ids": response_ids_from_evidence(negative_evidence),
            "excerpts": negative_evidence,
        }
    )

    engine_scores = scores["by_engine"]
    weakest_engine = None
    weakest_value = None
    for engine, engine_score in engine_scores.items():
        component = engine_score["components"]["branded_inclusion"]
        value = component["value"]
        if value is None:
            continue
        if weakest_value is None or value < weakest_value:
            weakest_engine = engine
            weakest_value = value
    engine_names = {"aimode": "Google AI Mode", "chatgpt": "ChatGPT", "gemini": "Gemini"}
    engine_label = engine_names.get(weakest_engine, weakest_engine or "the engines")
    engine_row = engine_scores.get(weakest_engine, {}) if weakest_engine else {}
    engine_component = engine_row.get("components", {}).get("branded_inclusion", {"numerator": 0, "denominator": 0})
    brand_citation_component = engine_row.get("components", {}).get("brand_citation", {"numerator": 0, "denominator": 0})
    engine_results = [
        result
        for result in results
        if result["status"] == "ok" and result["engine"] == weakest_engine and prompts[result["prompt_id"]]["branded"] and not result["labels"]["mentioned"]
    ]
    engine_evidence = evidence_items(engine_results, lambda item: True, 3)
    if not engine_evidence:
        engine_evidence = evidence_items(
            [
                result
                for result in results
                if result["status"] == "ok" and result["engine"] == weakest_engine and prompts[result["prompt_id"]]["branded"]
            ],
            lambda item: True,
            2,
        )
    entity_confusion = any(
        "Kaspr" in item["quote"] and "Kasparro" not in item["quote"] for item in engine_evidence
    )
    engine_explanation = (
        "The difference can come from each engine's retrieval mix, the sources available to it, or how the "
        "question is interpreted. The saved answers alone do not identify the cause."
    )
    if entity_confusion:
        engine_explanation += (
            " Some excerpts discuss Kaspr rather than Kasparro, so the captured answers also show a possible "
            "entity-confusion pattern."
        )
    diagnostics.append(
        {
            "observation": (
                f"{engine_label} named Kasparro in {engine_component['numerator']} of {engine_component['denominator']} "
                f"valid branded answers and returned a Kasparro citation in {brand_citation_component['numerator']} of "
                f"{brand_citation_component['denominator']} mentioning answers."
            ),
            "possible_explanation": engine_explanation,
            "test_evidence": (
                "Inspect the citation domains and source panels for this engine, compare them with the other engines on "
                "the same prompts, and check whether Kasparro's pages are accessible and useful to those sources."
            ),
            "response_ids": response_ids_from_evidence(engine_evidence),
            "excerpts": engine_evidence,
        }
    )
    return diagnostics


def build_remedies(diagnostics, scores):
    engine_names = {"aimode": "Google AI Mode", "chatgpt": "ChatGPT", "gemini": "Gemini"}
    overall = scores["overall"]
    discovery = overall["components"]["unprompted_discovery"]
    recommendation = overall["components"]["recommendation"]
    negative = overall["components"]["non_negative"]
    by_engine = scores["by_engine"]
    weakest_engine = None
    weakest_citation = None
    for engine, engine_score in by_engine.items():
        component = engine_score["components"]["brand_citation"]
        if component["value"] is None:
            continue
        if weakest_citation is None or component["value"] < weakest_citation:
            weakest_engine = engine
            weakest_citation = component["value"]
    engine_component = by_engine.get(weakest_engine, {}).get("components", {}).get("brand_citation", {"numerator": 0, "denominator": 0})

    return [
        {
            "problem": "Kasparro is absent from unprompted category-level questions.",
            "evidence": (
                f"Unprompted discovery is {discovery['numerator']}/{discovery['denominator']}, while branded inclusion is "
                f"{overall['components']['branded_inclusion']['numerator']}/{overall['components']['branded_inclusion']['denominator']}. "
                f"See diagnostic 1 response IDs: {', '.join(diagnostics[0]['response_ids']) or 'none'}."
            ),
            "action": (
                "Audit the pages and third-party sources that answer the unbranded question themes. Prioritise crawlable, "
                "factual comparison and explainer content with clear entity information, then seek relevant independent citations."
            ),
            "benefit": "More opportunities to be retrieved and cited when the buyer question does not name Kasparro.",
            "effort": "Medium",
            "uncertainty": (
                "High. This is one historical capture, and the answers do not prove why Kasparro is absent or that content "
                "changes will alter an engine's response."
            ),
            "validation_step": (
                "Rerun the same 25 unbranded prompts after a fixed interval and compare the unprompted mention rate with "
                f"the current {discovery['numerator']}/{discovery['denominator']} baseline. Require a repeatable change, not one answer."
            ),
        },
        {
            "problem": "Branded answers often describe Kasparro without recommending it, and some carry credibility doubt.",
            "evidence": (
                f"Recommendation is {recommendation['numerator']}/{recommendation['denominator']} and non-negative "
                f"characterisation is {negative['numerator']}/{negative['denominator']}. "
                f"See diagnostic 2 response IDs: {', '.join(diagnostics[1]['response_ids']) or 'none'}."
            ),
            "action": (
                "Review the cited pages behind the negative and non-recommending answers. Add verifiable case studies, "
                "method notes and dated evidence that answer the specific doubts, and correct any outdated claims."
            ),
            "benefit": "A clearer evidence trail for accurate representation when Kasparro is named.",
            "effort": "Medium",
            "uncertainty": (
                "High. The labels record wording in the answers, not the true cause. Some cited sources may be outside "
                "Kasparro's control."
            ),
            "validation_step": (
                "Rerun the same branded prompts and compare the recommendation rate and negative-label rate. Inspect "
                "whether the cited sources now contain the new evidence."
            ),
        },
        {
            "problem": f"{engine_names.get(weakest_engine, 'The weakest engine')} has the lowest brand-citation rate in this snapshot.",
            "evidence": (
                f"Brand citations for {engine_names.get(weakest_engine, 'that engine')} are {engine_component['numerator']}/{engine_component['denominator']} "
                f"among mentioning answers. See diagnostic 3 response IDs: {', '.join(diagnostics[2]['response_ids']) or 'none'}."
            ),
            "action": (
                "Inspect the source panels and citation domains for this engine on the same prompts. Check crawl access, "
                "structured data, internal links and whether the cited pages contain the claims an answer would need."
            ),
            "benefit": "Potential improvement in engine-specific representation and citation presence.",
            "effort": "Low to medium",
            "uncertainty": (
                "High. Engine retrieval differs, and a citation does not prove that a source supports a claim. One capture "
                "cannot establish a persistent engine effect."
            ),
            "validation_step": (
                "Rerun the same prompts on this engine and compare brand-citation presence with the current baseline. "
                "Inspect any new source before treating it as support."
            ),
        },
    ]


def build_report(dataset, prompts, responses, citations, results, analysis_id, mode, calls=None):
    rows = make_rows(results, prompts)
    scores = {
        "overall": score_rows(rows),
        "by_engine": grouped_scores(rows, lambda row: row["engine"]),
        "by_category": grouped_scores(rows, lambda row: row["category"]),
        "by_branded": grouped_scores(rows, lambda row: "branded" if row["branded"] else "unbranded"),
        "by_engine_category": engine_category_scores(rows),
    }
    diagnostics = build_diagnostics(results, prompts, scores)
    remedies = build_remedies(diagnostics, scores)
    counts = {
        "attempted": len(results),
        "valid": sum(1 for result in results if result["status"] == "ok"),
        "failed": sum(1 for result in results if result["status"] == "failed"),
        "skipped": sum(1 for result in results if result["status"] == "skipped"),
    }
    counts["coverage"] = round(counts["valid"] / counts["attempted"], 4) if counts["attempted"] else None
    return {
        "title": "Kasparro AI visibility snapshot",
        "analysis_id": analysis_id,
        "dataset_id": dataset["dataset_id"],
        "run_id": dataset["run_id"],
        "generated_at": db.utc_now(),
        "execution_mode": mode,
        "execution": {
            "mode": mode,
            "model": os.getenv("LLM_MODEL") or None,
            "classification_calls": calls if calls is not None else len(results),
            "cost_measurement": "not measured",
        },
        "versions": {
            "score": SCORE_VERSION,
            "categories": CATEGORY_VERSION,
            "prompt_set": dataset["run_id"],
            "classifier_prompt": "classify-v1",
        },
        "coverage": counts,
        "scores": scores,
        "prompt_comparison": prompt_comparison(prompts, results),
        "diagnostics": diagnostics,
        "remedies": remedies,
        "limitations": [
            "One captured answer per prompt and engine; this is a snapshot, not a repeated-run estimate.",
            "Model versions are provider-reported and some are unknown; unknown values remain unknown.",
            "Citation presence is observable, but source support was not verified by opening every page.",
            "Negative and recommendation labels are model judgments, not ground truth.",
            "No ranking improvement is promised by any remedy.",
        ],
        "results": results,
    }


def write_results_json(report):
    path = os.getenv("KASPARRO_RESULTS", os.path.join(db.BASE_DIR, "results.json"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return path


def run_analysis(conn, dataset_id, analysis_id, mode):
    records = db.load_records(conn, dataset_id)
    if records is None:
        raise ValueError("dataset not found")
    prompts = records["prompts"]
    responses = records["responses"]
    citations = records["citations"]
    max_calls = int(os.getenv("LLM_MAX_CALLS", "150"))

    db.start_analysis(conn, analysis_id)
    results = []
    calls = 0
    started_at = time.monotonic()
    try:
        for response in responses:
            response_with_citations = dict(response)
            response_with_citations["citations"] = citations.get(response["response_id"], [])
            prompt = prompts[response["prompt_id"]]
            if calls >= max_calls:
                results.append(
                    {
                        "response_id": response["response_id"],
                        "prompt_id": response["prompt_id"],
                        "engine": response["engine"],
                        "status": "skipped",
                        "reason": "call_budget_exhausted",
                        "labels": {"absent": False, "mentioned": False, "recommended": False, "negative": False},
                        "position": None,
                        "competitors": [],
                        "evidence": [],
                        "citations": citation_summary(response_with_citations["citations"]),
                        "mode": mode,
                        "trace": {"mode": mode, "reason": "call_budget_exhausted"},
                        "error": None,
                    }
                )
            else:
                result = classify_one(response_with_citations, prompt, mode)
                results.append(result)
                calls += 1
            db.save_result(conn, analysis_id, results[-1])

        counts = {
            "attempted": len(results),
            "valid": sum(1 for result in results if result["status"] == "ok"),
            "failed": sum(1 for result in results if result["status"] == "failed"),
            "skipped": sum(1 for result in results if result["status"] == "skipped"),
        }
        report = build_report(records["dataset"], prompts, responses, citations, results, analysis_id, mode, calls)
        report["execution"]["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
        report["active_time"] = os.getenv("KASPARRO_ACTIVE_TIME", "").strip() or "not recorded"
        db.finish_analysis(conn, analysis_id, "completed", counts)
        write_results_json(report)
        return {"counts": counts, "report": report, "results": results, "calls": calls}
    except Exception as exc:
        db.finish_analysis(conn, analysis_id, "failed", error=str(exc))
        raise

# Decision note

## Scoring and aggregation

I used a 0-100 score with five explicit rates: unprompted discovery (45%), branded inclusion (20%), recommendation (20%), non-negative characterisation (10%) and brand citation presence (5%). The largest weight goes to unbranded prompts because the data shows 72/75 branded answers naming Kasparro but 0/75 unbranded answers doing so. Weighting branded recognition heavily would make a mostly prompted result look like discovery.

Every component keeps its numerator and denominator. Failed and skipped responses are excluded from denominators but shown next to the score, so a failed analysis is never a zero-score observation. A component with no eligible observations is null, and the composite is withheld until all five denominators exist. The same function produces overall, engine, category and branded/unbranded scores. Prompt categories are `categories_v1`, inferred from the actual question text. Branded versus unbranded is a separate dimension, not a category.

## Backend, review and reliability

SQLite is used directly with a small schema and helpers, no ORM. Ingestion validates every record with `jsonschema`, checks relationships, and writes in one transaction. Raw JSON is retained. Re-importing the same run reuses the dataset; identical repeated IDs are reused, while a changed record with the same ID returns 409. That avoids silent duplication without throwing away a safe repeat import.

The API is intentionally bounded and synchronous. That is adequate for 150 replay records and easy to explain, but a real deployment would move model calls to a queue and add rate limits. Analysis results are stored per response before the next record is processed, so one bad model call becomes a visible failed result rather than erasing the run.

Review is a versioned state machine: draft, submitted, approved, rejected. The analyst token imports, analyses and edits. The reviewer token approves or rejects a submitted version with a comment. The role comes only from the token; request models forbid extra fields. Submitting freezes the version. Editing a submitted or approved version creates a new draft. Export without a version refuses anything that is not the current approved version. An explicitly requested old approved version can be exported, marked as not current. Every submit, approve and reject is recorded with actor role, version, comment and time.

The LLM adapter defaults to replay. Real `live` and `local` modes use an OpenAI-compatible chat-completions endpoint. Exact mention, citation counting, validation and permissions stay in code. Model output is checked for structure, exact evidence substrings, consistent labels and bounded size. Timeouts, retries, total calls, input size and output size are bounded. Every result and trace records its execution mode. Replay is not presented as evidence of live-model quality.

## Alternatives considered

I considered an ORM, a background worker, equal score weights and using the model for mention detection. I rejected them because the assignment rewards a correct, explainable basic flow; exact mention matching is more reliable than a model label; and the scoring needs to expose the unbranded gap. I also considered making every category score use only its applicable components, but all five categories contain both branded and unbranded prompts, so the common formula remains comparable.

## Top remedy

The top remedy is to investigate and improve unprompted discovery. The recorded answers name Kasparro only when the prompt supplies the name. The next action is an audit of the pages and third-party sources behind the 25 unbranded question themes, followed by a repeat of the same prompts after a fixed interval. The validation target is a repeatable rise in unprompted mentions, not one changed answer. I do not claim that content changes will cause ranking or inclusion gains.

## Failure, diagnosis and fix

The first replay classifier treated "If you're considering Kasparro specifically..." as a recommendation because `considering` was a broad cue. The diagnosis was that a conditional consideration does not show that the answer presents Kasparro as suitable. I narrowed the recommendation rule to explicit recommendation or suitability language and kept response `ac5bdf24-6a6e-49a0-abf5-2755ff3b42bc` as a regression check in `evaluate.py`. The evaluation report prints its expected and actual labels.

The first 12-case evaluation then exposed two remaining replay weaknesses. It missed the positive suitability sentence in `b40168e8-acf4-4430-b22d-8c484fab846f` because the cue sits in a following sentence, and it treated `b7768287-1be1-4089-bbbd-c0908289b0b9` as negative because the answer contains "not inherently a gimmick" while also missing the word "useful" as a recommendation cue. The negative false positive is visible in the final split. I left these disagreements in `eval/report.md` rather than tuning the cue list against twelve cases. The final 14-case sample adds two citation-positive cases and citation expectations on two existing cases. They were added for coverage, not to change the classifier. These remaining disagreements are limits of a deterministic replay classifier, not evidence about a live model.

## Largest risk and omissions

The largest risk is over-reading a single historical capture. One answer per prompt and engine cannot establish a persistent ranking, and the model labels are not ground truth. Some Gemini answers appear to discuss Kaspr, a different product, so the data also contains a possible entity-confusion pattern. Citation presence is not source support; the pages were not all inspected.

Omissions and unfinished work: `eval/labels.json` now contains 14 labelled cases, split eight tuning and six final, with two edge cases and four citation-presence checks. The evaluator exits 0 and the report includes per-label metrics, citation checks, remedy traceability checks, score arithmetic checks and evidence checks. The sample is small and was drafted with AI assistance, so the candidate must verify each label before treating the report as their own judgement. `eval/review_sample.md` contains the selected prompt, answer and current label for that check. No live-model run was made; no dashboard, Docker, vector search, multi-agent setup, crawling, deployment, repeated-run study, async job queue, rate limiting, review expiry, or simultaneous-review protection was added. The supplied data folders are not committed.

Future review work would add an expiry policy, for example an approval that lapses after 14 days, and a compare-and-set version check so two reviewers cannot decide the same version concurrently. Neither is implemented.

## Questions before release

1. Which hosted or local model and endpoint are approved for a real-model run?
2. Should the reviewer approval expire after a fixed period, and who may export?
3. Is JSON export sufficient, or is a specific report format required?
4. Should diagnostic and remedy edits be free text, or remain fully structured?
5. Should each human user get a separate token rather than one shared analyst and one shared reviewer token?

## AI assistance

This implementation and the committed evaluation sample were drafted with an AI coding assistant. The candidate must review the code and labels, be able to explain every line, and verify the claims in the decision note. Tests are deterministic and were run locally. No credentials are stored in the repository.

## Work log for time accounting

| Stage | Work done |
|---|---|
| Pack and data reading | Read the brief, schema, data guide and fixtures; checked counts and duplicate/entity issues |
| Exploration | Counted mentions by engine, branded split, categories and citation deduplication |
| Build | SQLite schema, ingestion, replay adapter, classification, scoring, API and review state machine |
| Tests | Invalid input, duplicate reuse, timeout, malformed output, wrong role, rejection and edit-after-approval |
| Evaluation | 14-case sample, baseline/LLM comparison, citation checks, remedy traceability, score arithmetic check, evidence checks and regression note |
| Documentation | README, environment example, requirements and this note |

The candidate confirmed an active-time total of approximately 5 hours across the stages above. Stage-level splits were not recorded separately. This is within the six-hour limit.

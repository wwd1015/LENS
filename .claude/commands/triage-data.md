---
description: Run TabPFN-TS anomaly detection on a dataset, then investigate root cause analyst-style.
argument-hint: <dataset-name> [--field FIELD] [--context-window N] [--score-threshold N]
---

# /triage-data

Two-phase anomaly triage: zero-shot detection via TabPFN-TS, then root-cause investigation by walking lineage and the producing-code git history.

This skill is project-agnostic. Project-specific knowledge (datasets, lineage, owners, code paths) lives in `LINEAGE.yaml` at the repo root — never in this prompt. See `docs/LINEAGE.md` for the schema.

The skill never auto-acts. It writes `ROOT_CAUSE.md` for the analyst to read and decide.

---

## Inputs

`$ARGUMENTS` may include:
- `<dataset-name>` (required) — must match a `datasets:` entry in `LINEAGE.yaml`
- `--field FIELD` — column to monitor (overrides the dataset's default field)
- `--context-window N` — history length passed to TabPFN-TS (default 90)
- `--score-threshold N` — |z-score| above which a row is flagged (default 3.0)

If `LINEAGE.yaml` is missing, stop and tell the user to author one. Do not invent lineage.

---

## Phase 1 — Detect

1. Read `LINEAGE.yaml`. Find the entry for `<dataset-name>`. Extract: source path, entity_col, snapshot_col, default monitored fields, upstream tables, producing-code path.
2. Build a one-off Python invocation that:
   - Loads the dataset via `lens.io.PolarsSource`
   - Constructs a `Suite` with one `TabPFNAnomalyCheck` per monitored field
   - Runs the suite
   - Writes the resulting `Issue` list as `anomalies.json` (use `dataclasses.asdict` per issue)
3. Run it with `python3 -c '...'` or a temp script under `scripts/_triage_<dataset>.py`.
4. Read `anomalies.json`.
   - **Empty** → write a one-line `ROOT_CAUSE.md` saying "No anomalies detected on `<dataset>` at `<run_time>`" and exit clean.
   - **Non-empty** → continue to Phase 2.

If the TabPFN extra is not installed, the check raises `ImportError` pointing at `pip install -e ".[tabpfn]"`. Surface that to the user and stop. Do not silently fall back to a non-TabPFN check.

---

## Phase 2 — Investigate

Goal: produce a written hypothesis for *why* the anomaly happened. Mimic an analyst — hold data, lineage, and code in one head simultaneously.

For each distinct `(entity_id, field_name)` pair flagged in `anomalies.json` (group, don't enumerate every row):

1. **Characterize the anomaly.**
   - Read the dataset around the anomalous snapshot (Read or DuckDB via Bash).
   - Decide its shape: sudden spike, slow drift, level shift, missing values, schema-shaped.
   - Check whether the anomaly is isolated to one entity or affects many — if many, the cause is likely upstream (pipeline / source) rather than entity-specific.

2. **Walk lineage upward.**
   - From `LINEAGE.yaml`, identify upstream tables.
   - For each upstream, run the same `TabPFNAnomalyCheck` on the same date range. Record which upstreams are clean and which also flag.
   - The deepest upstream that flags is the most likely point of injection.

3. **Code archaeology on the producing path.**
   - From `LINEAGE.yaml`, get the producing-code path for `<dataset>` (or for the deepest flagged upstream).
   - `git -C <repo> log --since='<N>.days.ago' --oneline -- <path>` for recent commits scoped to that path.
   - For each suspicious commit, `git show <sha> -- <path>` and reason about whether the diff could plausibly produce the observed anomaly shape.

4. **Form a hypothesis.**
   - Write `ROOT_CAUSE.md` with the structure below. Be explicit about confidence and what evidence would raise or lower it.

### `ROOT_CAUSE.md` structure (required)

```markdown
# Root Cause Hypothesis: <dataset> @ <run_time>

## Summary
One sentence: what's anomalous and the leading hypothesis.

## Anomaly
- Dataset: <dataset>
- Field(s): <list>
- Entities affected: <count>, sample IDs: <list>
- Snapshot range: <from> → <to>
- Shape: <spike | drift | level-shift | schema | other>
- Severity (TabPFN |z|): <max score>

## Lineage walked
| Layer | Table | TabPFN flag? | Notes |
|---|---|---|---|
| this | <dataset> | yes | observed |
| up-1 | <upstream> | yes/no | ... |
| up-2 | <upstream> | yes/no | ... |

## Candidate causes considered
1. **<cause>** — evidence for / against. Verdict: <kept | ruled out>.
2. ...

## Recommended hypothesis
<2–4 sentences. State the cause, the layer it injects at, and the next verification step a human should take.>

## Confidence
<low | medium | high> — and one sentence on what would change it.

## Evidence appendix
- Anomaly excerpts: <inline or path>
- Suspicious commits: <SHAs + one-line summaries>
- Upstream check outputs: <paths>
```

---

## Constraints

- **Never** open a ticket, send a notification, or push code from this skill. Output is `ROOT_CAUSE.md` and `anomalies.json` only.
- **Never** modify the dataset, the producing code, or `LINEAGE.yaml`.
- **Never** invent lineage that isn't in `LINEAGE.yaml`. If lineage is incomplete, say so in `ROOT_CAUSE.md` and stop walking.
- **Never** use a non-TabPFN fallback silently. If the extra is missing, fail loud.
- Keep the investigation bounded: at most 3 lineage layers up, at most 30 days of git history per code path. If the trail goes deeper, write what you found and flag the gap.

---

## Anti-patterns

- Writing project-specific lending logic into this prompt. (Belongs in `LINEAGE.yaml` and dataset-specific config.)
- Treating one flagged entity as the whole story when many are flagged. (If many: look upstream first.)
- Confabulating a commit-to-anomaly link without showing the diff. (Always cite the SHA and the relevant lines.)
- Padding `ROOT_CAUSE.md` with restated context. (The summary + hypothesis sections are the value; everything else is evidence.)

---

## Success criteria

- `anomalies.json` exists and reflects a real `Suite` run, not a hand-authored stub.
- `ROOT_CAUSE.md` ends with a falsifiable hypothesis the analyst can verify in <15 minutes.
- Confidence is calibrated: the report says "low" when the lineage is incomplete or no commit fits, not "high" by default.

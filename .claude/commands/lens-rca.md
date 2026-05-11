---
description: Investigate root cause for a LENS finding — re-investigate an existing flag or run a fresh investigation on an analyst-supplied (entity, field, date).
argument-hint: (--finding-id <id> [--run-id <id>] | --investigate-entity <id> --field <name> --date <YYYY-MM-DD>) [--data <path>]
---

# /lens-rca

Per-finding root-cause analysis on demand. Wraps `lens.rca.agent.RCAAgent` so an
analyst can ask "why did this happen?" interactively — without re-running the
full detection orchestrator.

This skill is invoked INTERACTIVELY. If the wiki is ambiguous (e.g. multiple
producing-code paths), ask the analyst before scanning.

---

## Two modes (exactly one required)

**Re-investigate mode** — an orchestrator run already flagged this point:
```
/lens-rca --finding-id <id> [--run-id <id>] [--data <path>]
```
Looks up the Finding in `findings.latest.json` (or `findings.<run_id>.json` if
`--run-id` is given), then runs RCA on it.

**Fresh-investigate mode** — no prior detection; ad-hoc question (investor ask,
post-incident, "this number looks wrong"):
```
/lens-rca --investigate-entity <entity_id> --field <field_name> --date <YYYY-MM-DD> [--data <path>]
```
Synthesizes a Finding-like wrapper (no detection happened; `detector_sources=["ad_hoc"]`)
and invokes RCA on it directly.

If neither or both modes are specified, stop and tell the analyst which to pick.

---

## Procedure

1. **Parse `$ARGUMENTS`.** Determine mode. Validate the required args for that mode.
   Reject if both `--finding-id` and `--investigate-entity` are present.

2. **Load the wiki.** Read `lens-wiki/index.md`, then enumerate `lens-wiki/datasets/*.md`,
   `lens-wiki/rules/*.md`, `lens-wiki/lineage/*.md`. Build a mental index of which
   tables/fields each page references. If `lens-wiki/` does not exist, stop and
   tell the analyst to author it (the RCA agent depends on it).

3. **Resolve the Finding.**
   - *Re-investigate*: open `findings.latest.json` (or `findings.<run_id>.json`). Find
     the Finding whose `issue.finding_id` equals the supplied id. If absent, stop
     and report. Capture `run_id` from the file (or the user flag).
   - *Fresh-investigate*: build a synthetic Finding:
     `Issue(check_name="ad_hoc", severity=Severity.WARNING, entity_id, field_name,
     snapshot_date=parsed-date, detector_source="ad_hoc",
     finding_id=compute_finding_id(entity, field, date))` wrapped in
     `Finding(issue=..., detector_sources=["ad_hoc"], detected_at=now, run_id="ad_hoc-<timestamp>")`.

4. **Resolve the data.** Identify the source LazyFrame(s) for the relevant table.
   Prefer `--data <path>` if supplied. Otherwise infer from the dataset page's
   frontmatter / body in `lens-wiki/datasets/`. If still ambiguous, ASK the analyst.

5. **Walk lineage + code.** From the matching `lens-wiki/lineage/*.md`, list
   `producing_code` paths. For each, run `git log --follow --no-merges -n 5 -- <path>`.
   Surface the SHA, date, subject, and (if remote is parseable) the commit URL.
   Do NOT confabulate a commit-to-anomaly link without showing the diff.

6. **Invoke the RCA agent.** Use `lens.rca.agent.RCAAgent` directly:
   ```python
   from lens.rca.agent import RCAAgent
   from lens.wiki.cache import WikiCache
   agent = RCAAgent(repo_root=Path("."), output_dir=Path("."))
   rca = agent.investigate(finding, WikiCache.from_dir("lens-wiki"), sources)
   out = agent.save(rca, run_id=finding.run_id)
   ```
   The agent handles sampling, prompt construction, LLM call, and JSON-on-disk write
   to `rca/<run_id>/<finding_id>.json`.

7. **Print the result inline.** Three sections, in this order:
   - **Hypothesis** — 1–2 paragraphs. State the cause, the layer it injects at.
   - **Evidence** — bulleted, observable facts (anomalous-vs-contrast row deltas,
     specific commits, rule violations). One bullet per fact.
   - **References** — URLs and paths (commit URLs from the RCA, lineage page paths,
     rule page paths). The analyst should be able to click each.

8. **Suggest next steps** — 2–3 concrete actions the analyst could take:
   verify a specific commit's diff, ask the table owner a specific question, re-run
   detection on the upstream table over the same window, etc.

---

## Style

- **Be concrete.** Name specific commits (12-char SHA + URL), file paths, contrast
  rows. Never say "recent changes may have caused this" without naming the change.
- **Calibrate confidence.** `0.9+` = smoking gun (diff visibly produces the anomaly
  shape). `0.5` = one plausible explanation, no smoking gun. `<0.3` = guessing;
  say "evidence is weak — recommend the analyst investigate manually."
- **One Finding per invocation.** This is interactive triage, not batch.

---

## Constraints

- **Never** modify the data, the producing code, or the wiki.
- **Never** open tickets or send notifications.
- **Never** invent lineage that isn't in `lens-wiki/`. If a page is missing, say so.
- **Output is** the inline conversation summary plus `rca/<run_id>/<finding_id>.json`
  on disk. Nothing else.

---

## Success criteria

- The printed hypothesis is falsifiable in <15 minutes by a human.
- Every commit mentioned in evidence has its SHA and URL surfaced.
- Confidence reflects the evidence — low when lineage or commit history is sparse,
  high only when a diff visibly produces the observed anomaly shape.
- The JSON on disk matches the schema written by the orchestrator-invoked RCA so
  downstream consumers (briefer, feedback log) treat both paths identically.

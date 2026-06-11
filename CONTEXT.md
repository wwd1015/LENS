# LENS

Data quality surveillance for commercial lending data: detects anomalies in lending
datasets across time, across sources, and at point-in-time, then explains and reports
them to analysts.

## Language

### Detection

**Detector**:
A pluggable unit of anomaly detection, registered by name and run by the engine —
from simple assertions (nulls, ranges) to statistical models (STL, TabPFN). A single
detector may stamp namespaced identities (a detector family), e.g. one per wiki rule.
_Avoid_: check (legacy class names only), validator, test

**Issue**:
A single anomalous data point observed by one detector — one entity, one field, one
snapshot date. The raw, pre-scoring unit of detection.
_Avoid_: anomaly, alert, error

**Finding**:
The deduplicated, scored record of an anomalous data point. Many Issues at the same
(entity, field, snapshot date) collapse into one Finding, which remembers every
detector that flagged it.
_Avoid_: issue (that's the pre-dedup unit), alert

**Finding Group**:
All Findings that share the same detector family and field. The unit at which the
brief renders sections and at which batch root-cause investigation runs — one
investigation per group.
_Avoid_: cluster, incident, bucket

### Investigation & reporting

**RCA (Root-Cause Analysis)**:
An LLM-driven investigation of a Finding Group that produces a hypothesis about why
the anomaly happened, grounded in wiki pages, recent commits, and sampled data.
_Avoid_: triage, diagnosis

**Brief**:
The analyst-facing report of one batch run — the prioritized, grouped view of
Findings with their RCA hypotheses. Exists in HTML (full) and markdown (digest) forms.
_Avoid_: report, dashboard, digest (the markdown form is "the digest", a view of the Brief)

**Feedback**:
An analyst's verdict on a Finding: real, false positive, or needs more investigation.
_Avoid_: label, annotation

### Knowledge base

**Wiki**:
The repository of curated knowledge about the data estate (`lens-wiki/`): what each
dataset is, how sources must reconcile, and what code produces what. The source of
truth for cross-source rules.
_Avoid_: docs, metadata store

**Rule**:
A wiki page declaring how values across sources must reconcile, expressed as a
structured equation with a tolerance.
_Avoid_: check (that's executable code), constraint, validation

**Dataset Page / Lineage Page**:
Wiki pages describing, respectively, one table (grain, segments) and one
producing-code path.

### Entry points

**Batch Run**:
A scheduled end-to-end execution: detect across all sources, investigate Finding
Groups above a severity floor, render the Brief. The "morning brief" path.
_Avoid_: pipeline run, cron job

**Ad-hoc Investigation**:
An analyst-triggered RCA on one Finding or one entity/field/date, run from a Claude
Code session without a Batch Run.
_Avoid_: manual run

## Flagged ambiguities

- **Check vs. Detector** — resolved: **Detector** is canonical in all prose, docs,
  and new code. "Check" survives only in legacy identifiers (`BaseCheck`,
  `CheckResult`, `checks/`) until a mechanical rename is worthwhile.

## Example dialogue

> **Dev:** The volatility check fired on 40 entities for `outstanding_balance` yesterday.
> **Analyst:** So 40 Issues — did they survive dedup?
> **Dev:** STL flagged 35 of the same points, so after dedup it's 40 Findings, each
> listing both detectors. They all land in one Finding Group since it's one field.
> **Analyst:** Then the Batch Run should have produced one RCA for the group, not forty.
> **Dev:** Right — the Brief shows the group with its single hypothesis: a backfill
> commit touched the producing path. I marked the group's top Finding as real via Feedback.

# Batch RCA runs once per Finding Group above a severity floor, not per Finding

The scheduled batch run investigates Finding Groups — findings sharing
(detector family, field), the same key the brief already renders sections by — at or
above a configurable severity floor, passing the member entity list as context. We
rejected per-finding RCA because every investigation is a `claude -p` subprocess
call: one upstream break fanning out to hundreds of entities would turn the morning
cron into hours of near-identical LLM calls. One group = one root-cause hypothesis,
attached to exactly one brief section. The trade-off: genuinely distinct root causes
on different entities of the same (detector, field) share a single investigation;
analysts escalate those via ad-hoc `/lens-rca` on the specific finding.

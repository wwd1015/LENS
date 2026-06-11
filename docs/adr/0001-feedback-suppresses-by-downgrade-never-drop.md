# Feedback suppresses by downgrading to INFO with expiry, never by dropping

A false-positive verdict in feedback.jsonl downgrades future findings on the same
(entity, field, detector) to INFO for a configurable window (default 90 days); it
never removes them from findings.json. We chose this over hard-dropping because a
suppressed series can later break for real — downgraded findings stay visible in a
collapsed brief section and resurface automatically when the verdict expires, and
downstream consumers (run deltas, resolved counts) never see phantom resolutions.
The cost is a noisier findings.json and expiry logic; we accepted it to guarantee
nothing is ever silently hidden.

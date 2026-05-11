---
name: <table-slug>.lineage
description: Upstream and downstream lineage for <table-slug>
table: <table-name>
upstream:
  - table: <upstream-table-1>
    via: <producing-code-path>
    relationship: <e.g., one-to-one, aggregation, filter>
downstream:
  - table: <downstream-table-1>
    via: <consuming-code-path>
producing_code:
  - <relative/path/to/code.sql>
  - <relative/path/to/transform.py>
source_commit: <git-sha>
last_updated: <YYYY-MM-DD>
---

# Lineage: <table-slug>

## Upstream
<For each upstream table: how data flows in. The RCA agent walks this when investigating anomalies.>

## Downstream
<Where this table feeds. Useful when assessing blast radius.>

## Producing code
<Files that compute this table. The RCA agent will `git log --follow --no-merges` these paths when hunting for change-correlated anomalies.>

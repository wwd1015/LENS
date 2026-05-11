# LENS: Longitudinal & Entity-level Normative Surveillance

A data quality control tool for commercial lending data, built on Polars.

## Install

```bash
pip install -e ".[dev]"          # development
pip install -e ".[snowflake]"    # with Snowflake support
```

## Quick Start

```python
import polars as pl
from lens.engine import Suite
from lens.io import PolarsSource

source = PolarsSource(path="loans.parquet", entity_col="loan_id", snapshot_col="as_of_date")

suite = Suite(entity_col="loan_id", snapshot_col="as_of_date")
suite.add("null_check", fields=["balance", "status"])
suite.add("stale_data", field="balance", max_unchanged=30)
suite.add("monotonicity", field="cumulative_payments", direction="increasing")
suite.add("volatility", field="balance", max_pct_change=0.5)

result = suite.run(source)
print(result.summary)
for issue in result.all_issues:
    print(issue)
```

Suites can also be defined in YAML — see `lens.config.load_suite`.

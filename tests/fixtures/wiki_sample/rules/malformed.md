---
name: malformed
description: Frontmatter is missing the closing fence so this page must be skipped.
tables:
  - whatever

# Rule: malformed

This file is intentionally broken — the YAML block above is never closed by
a `---` fence, so `parse_page` must log a warning and return None.

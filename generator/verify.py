"""Checks the pipeline's conversions against the generator's ground truth.

Usage (from generator/): python3 -m verify
Runs clickhouse/queries/verify.sql, prints the summary and any mismatched orders, and
exits with 1 unless the check passes. Connection settings come from CH_HOST, CH_PORT,
CH_USER and CH_PASSWORD, falling back to the repo's .env.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
QUERIES = REPO / "clickhouse" / "queries" / "verify.sql"


def statements(path: Path) -> List[str]:
    """Returns the SQL statements in `path`, comments removed."""
    sql = re.sub(r"--[^\n]*", "", path.read_text())
    return [s.strip() for s in sql.split(";") if s.strip()]


def env_file() -> Dict[str, str]:
    path = REPO / ".env"
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines()
             if "=" in line and not line.lstrip().startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def main() -> int:
    import clickhouse_connect

    env = env_file()
    client = clickhouse_connect.get_client(
        host=os.environ.get("CH_HOST", "localhost"),
        port=int(os.environ.get("CH_PORT", "8123")),
        username=os.environ.get("CH_USER", env.get("CH_USER", "default")),
        password=os.environ.get("CH_PASSWORD", env.get("CH_PASS", "")),
    )
    summary_sql, mismatches_sql = statements(QUERIES)
    summary = client.query(summary_sql)
    row = dict(zip(summary.column_names, summary.result_rows[0]))
    for name, value in row.items():
        print(f"{name:<10} {value}")

    mismatches = client.query(mismatches_sql)
    if mismatches.result_rows:
        print(f"\n{len(mismatches.result_rows)} mismatched orders (first 100):")
        for r in mismatches.result_rows:
            print("  " + "  ".join(f"{k}={v}" for k, v in zip(mismatches.column_names, r)))
    return 0 if row["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""
Run this against a real environment before going live to print the actual
column names in [marketing_bi].[EDA-268].[vw__demandbase_Opportunity_import].
Use the output to replace every CHANGEME__... placeholder in
field_mapping.yaml with real source_field values.

Usage:
    python scripts/introspect_view_schema.py --env dev
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from demandbase_sync.config import load_config
from demandbase_sync.db import fetch_view_schema, get_connection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["dev", "test", "prod"], default="prod")
    args = parser.parse_args()

    config = load_config(args.env)
    conn = get_connection(config)
    try:
        columns = fetch_view_schema(conn, config.database.view_fqname)
    finally:
        conn.close()

    print(f"Columns in {config.database.view_fqname}:")
    for col in columns:
        print(f"  - {col}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

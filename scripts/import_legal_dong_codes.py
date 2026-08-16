"""Import the official legal-dong full-data CSV downloaded from code.go.kr."""

from __future__ import annotations

import argparse
from pathlib import Path

from westbusan.buildings.load import load_legal_dong_codes
from westbusan.db import Database


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import active Busan legal-dong codes from the official full-data CSV"
    )
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--migrations-dir", type=Path, default=Path("sql"))
    args = parser.parse_args(arguments)
    db = Database(args.db_path, args.migrations_dir)
    db.migrate()
    print(load_legal_dong_codes(args.csv_path, db))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the command entry point.
    raise SystemExit(main())

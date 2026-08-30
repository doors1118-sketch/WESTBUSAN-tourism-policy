"""Build or validate a complete static tourism dashboard release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from westbusan.tourism_dashboard.release import (
    DashboardReleaseError,
    build_dashboard_release,
    validate_dashboard_release,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS = ROOT / "src" / "westbusan" / "tourism_dashboard" / "assets"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble a dashboard release only when all three maps exist."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Build one immutable release.")
    build.add_argument("--dashboard-assets", type=Path, default=DEFAULT_ASSETS)
    build.add_argument("--investment-map", type=Path, required=True)
    build.add_argument("--vacant-map", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate", help="Validate a built release.")
    validate.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "build":
            release = build_dashboard_release(
                dashboard_assets=args.dashboard_assets,
                investment_map=args.investment_map,
                vacant_map=args.vacant_map,
                output_directory=args.output,
            )
            result = {
                "status": "COMPLETED",
                "release": str(release.directory),
                "access_snapshot_id": release.access_snapshot_id,
                "file_count": release.file_count,
            }
        else:
            if not validate_dashboard_release(args.release):
                raise DashboardReleaseError("dashboard_release_invalid")
            result = {"status": "VALID", "release": str(args.release)}
    except DashboardReleaseError as error:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from error

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

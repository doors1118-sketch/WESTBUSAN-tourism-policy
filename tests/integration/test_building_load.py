import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from westbusan.accommodation.load import load_license_snapshot
from westbusan.accommodation.normalize import normalize_license
from westbusan.buildings import load as building_load
from westbusan.buildings.load import (
    collect_buildings_for_licenses,
    load_legal_dong_codes,
)
from westbusan.db import Database
from westbusan.models import ApiPage, RunContext
from westbusan.sources.registry import SourceRegistry


def test_same_parcel_is_requested_once_and_links_each_license(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "test.duckdb", Path("sql"))
    db.migrate()
    load_legal_dong_codes(Path("tests/fixtures/reference/legal_dong_codes.csv"), db)
    records = [
        normalize_license(
            "lodgings",
            {
                "MNG_NO": record_id,
                "LOTNO_ADDR": "부산광역시 서구 충무동1가 12-3",
            },
            datetime(2026, 8, 16, tzinfo=UTC).date(),
        )
        for record_id in ("BUSAN-1", "BUSAN-2")
    ]
    load_license_snapshot(db, records, RunContext.start("test", datetime.now(UTC)).run_id)
    title_rows = json.loads(
        Path("tests/fixtures/buildings/title.json").read_text(encoding="utf-8")
    )
    permit_rows = json.loads(
        Path("tests/fixtures/buildings/permit.json").read_text(encoding="utf-8")
    )
    calls: list[str] = []

    class FakePager:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def iter_url(self, url: str, *_: object, **__: object) -> list[ApiPage]:
            calls.append(url)
            rows = title_rows if url.endswith("getBrTitleInfo") else permit_rows
            return [
                ApiPage(
                    rows=rows,
                    total_count=len(rows),
                    page_no=1,
                    page_size=len(rows),
                    raw_body=b"{}",
                    schema_fingerprint="fixture",
                )
            ]

    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-key")
    monkeypatch.setattr(building_load, "DataGoKrPager", FakePager)

    result = collect_buildings_for_licenses(
        db, SourceRegistry.load(Path("config/sources.yaml")), RunContext.start("test", datetime.now(UTC))
    )

    assert calls.count("https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo") == 1
    assert result.bridge_rows == 2
    assert db.query("select count(*) from bridge_license_building") == [(2,)]

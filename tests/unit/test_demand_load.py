import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from westbusan.db import Database
from westbusan.demand.load import iter_months, load_tourism_demand, normalize_demand_row
from westbusan.models import ApiPage, RunContext, SourceSpec
from westbusan.sources.registry import SourceRegistry


def test_month_iterator_includes_end_month() -> None:
    """A missing final month would silently understate a backfill."""
    months = list(iter_months(date(2022, 1, 1), date(2022, 3, 31)))

    assert [str(month) for month in months] == ["2022-01", "2022-02", "2022-03"]


def test_demand_row_maps_saha_visitor_count_to_west() -> None:
    """A district mapping regression would put West Busan demand in the wrong region."""
    row = json.loads(
        (Path("tests/fixtures/demand/area_demand.json")).read_text(encoding="utf-8")
    )[0]
    record = normalize_demand_row("area_tourism_demand", row)

    assert record.period == "2026-01"
    assert record.district == "사하구"
    assert record.region_group == "west"
    assert record.metric_code == "visitor_count"
    assert record.metric_value == 1200


def test_consumption_row_keeps_lodging_spend_as_its_own_metric() -> None:
    """Treating consumption as visitors would corrupt its source-native grain."""
    row = json.loads(
        (Path("tests/fixtures/demand/area_consumption.json")).read_text(
            encoding="utf-8"
        )
    )[0]
    record = normalize_demand_row("area_tourism_consumption", row)

    assert record.metric_code == "lodging_consumption_amount"
    assert record.metric_value == 430000


def test_load_persists_raw_page_and_source_native_demand_record(
    tmp_path: Path, monkeypatch
) -> None:
    """Dropping raw evidence or replacing the metric grain must fail this load."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="area_tourism_demand",
                url="https://example.test/AreaTarDemDsService",
                operation="reviewedOperation",
                group="tourism",
                required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            assert service_key == "secret-service-key"

        def iter_pages(self, spec: SourceSpec, parameters: dict[str, object]):
            assert spec.url.endswith("/reviewedOperation")
            assert parameters == {"baseYm": "202601", "areaCd": "26"}
            yield ApiPage(
                rows=[{"baseYm": "202601", "signguNm": "사하구", "visitorCnt": "1200"}],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=(
                    '{"data":[{"baseYm":"202601","signguNm":"사하구","visitorCnt":"1200"}]}'
                ).encode(),
                schema_fingerprint="observed-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    result = load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert result.records_loaded == 1
    assert db.query(
        "select metric_code, period, district, region_group, metric_value from fact_tourism_demand"
    ) == [("visitor_count", "2026-01", "사하구", "west", 1200.0)]
    assert db.query("select count(*) from raw_artifact") == [(1,)]
    status = db.query("select detail_json from source_status")[0][0]
    assert '"baseYm": "202601"' in status
    assert "secret-service-key" not in status

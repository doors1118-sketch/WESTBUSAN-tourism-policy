import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from westbusan.db import Database
from westbusan.demand.load import (
    iter_collection_months,
    iter_months,
    load_tourism_demand,
    normalize_demand_row,
)
from westbusan.models import ApiPage, RunContext, SourceSpec
from westbusan.sources.registry import SourceRegistry, record_inspection


def test_month_iterator_includes_end_month() -> None:
    """A missing final month would silently understate a backfill."""
    months = list(iter_months(date(2022, 1, 1), date(2022, 3, 31)))

    assert [str(month) for month in months] == ["2022-01", "2022-02", "2022-03"]


def test_collection_plan_respects_current_and_bounded_source_history() -> None:
    """Backfilling current forecasts or unavailable related-tourism months is false history."""
    start = date(2022, 1, 1)
    end = date(2026, 1, 31)

    assert [
        str(item) for item in iter_collection_months("tourism_data_lab", start, end)
    ][:2] == ["2022-01", "2022-02"]
    assert [
        str(item)
        for item in iter_collection_months("tourism_concentration_rate", start, end)
    ] == ["2026-01"]
    related = [
        str(item)
        for item in iter_collection_months("related_tourism_destinations", start, end)
    ]
    assert related[0] == "2024-05"
    assert related[-1] == "2025-04"


def test_demand_row_maps_saha_stay_intensity_to_west() -> None:
    """A district mapping regression would put West Busan demand in the wrong region."""
    row = json.loads(
        (Path("tests/fixtures/demand/area_demand.json")).read_text(encoding="utf-8")
    )[0]
    record = normalize_demand_row("area_tourism_demand", row)

    assert record.period == "2026-01"
    assert record.district == "사하구"
    assert record.region_group == "west"
    assert record.metric_code == "stay_intensity_index"
    assert record.unit == "index"
    assert record.metric_value == 1.25


def test_consumption_row_keeps_lodging_spend_as_its_own_metric() -> None:
    """Treating consumption as visitors would corrupt its source-native grain."""
    row = json.loads(
        (Path("tests/fixtures/demand/area_consumption.json")).read_text(
            encoding="utf-8"
        )
    )[0]
    record = normalize_demand_row("area_tourism_consumption", row)

    assert record.metric_code == "tourism_service_demand_index"
    assert record.unit == "index"
    assert record.metric_value == 430000


@pytest.mark.parametrize(
    "source_id,row,metric_code,unit,period,dimensions",
    [
        (
            item["source_id"],
            item["row"],
            item["metric_code"],
            item["unit"],
            item["period"],
            item["dimensions"],
        )
        for item in json.loads(
            Path("tests/fixtures/demand/official_rows.json").read_text(encoding="utf-8")
        )
    ],
)
def test_official_kto_rows_preserve_source_specific_grain(
    source_id: str,
    row: dict[str, object],
    metric_code: str,
    unit: str,
    period: str,
    dimensions: dict[str, object],
) -> None:
    """A generic visitor/consumption mapping would corrupt KTO-native measures."""
    record = normalize_demand_row(source_id, row)

    assert record.metric_code == metric_code
    assert record.unit == unit
    assert record.period == period
    assert record.dimensions == dimensions


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
                operation="areaTarSjrnDsList",
                group="tourism",
                required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            assert service_key == "secret-service-key"

        def iter_pages(
            self,
            spec: SourceSpec,
            parameters: dict[str, object],
            *,
            include_empty: bool,
        ):
            assert include_empty is True
            assert spec.url.endswith("/areaTarSjrnDsList")
            assert parameters == {"baseYm": "202601", "areaCd": "26"}
            yield ApiPage(
                rows=[
                    {
                        "baseYm": "202601",
                        "signguNm": "사하구",
                        "sjrnDsValue": "1.25",
                    }
                ],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=(
                    '{"data":[{"baseYm":"202601","signguNm":"사하구","sjrnDsValue":"1.25"}]}'
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
    ) == [("stay_intensity_index", "2026-01", "사하구", "west", 1.25)]
    request_json, content_hash, source_date = db.query(
        "select request_json, content_hash, source_date from raw_artifact"
    )[0]
    raw_request = json.loads(request_json)
    assert raw_request["operation"] == "areaTarSjrnDsList"
    assert raw_request["parameters"] == {"areaCd": "26", "baseYm": "202601"}
    assert raw_request["pageNo"] == 1
    assert raw_request["schema_fingerprint"] == "observed-fields"
    assert raw_request["source_revision"] == content_hash
    assert source_date == date(2026, 1, 1)
    status = db.query("select detail_json from source_status")[0][0]
    assert '"baseYm": "202601"' in status
    assert "secret-service-key" not in status


def test_loader_uses_portal_reviewed_inspection_for_unresolved_source(
    tmp_path: Path, monkeypatch
) -> None:
    """Ignoring recorded review metadata would call no operation or the wrong one."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    unresolved = SourceSpec(
        source_id="area_tourism_demand",
        url="https://example.test/AreaTarDemDsService",
        group="tourism",
        inspection_required=True,
    )
    registry = SourceRegistry((unresolved,))
    record_inspection(
        unresolved,
        db,
        operation="areaTarSjrnDsList",
        required_parameters={
            "baseYm": "{baseYm}",
            "areaCd": "26",
            "signguCd": "26380",
        },
        response_row_path="response.body.items.item",
        portal_detail_url="https://www.data.go.kr/data/15151868/openapi.do",
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            assert service_key == "secret-service-key"

        def iter_pages(
            self,
            spec: SourceSpec,
            parameters: dict[str, object],
            *,
            include_empty: bool,
        ):
            assert include_empty is True
            assert spec.url.endswith("/areaTarSjrnDsList")
            assert parameters == {
                "baseYm": "202601",
                "areaCd": "26",
                "signguCd": "26380",
            }
            yield ApiPage(
                rows=[
                    {
                        "baseYm": "202601",
                        "signguNm": "사하구",
                        "sjrnDsValue": "1.25",
                    }
                ],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=b'{"data":[{"baseYm":"202601","sjrnDsValue":"1.25"}]}',
                schema_fingerprint="reviewed-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    result = load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert result.sources_ready == ("area_tourism_demand",)
    assert db.query("select metric_code, unit from fact_tourism_demand") == [
        ("stay_intensity_index", "index")
    ]


def test_nonempty_row_without_a_reviewed_metric_is_schema_changed(
    tmp_path: Path, monkeypatch
) -> None:
    """Calling an unexpected operation must not create a misleading READY status."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="area_tourism_demand",
                url="https://example.test/AreaTarDemDsService",
                operation="areaTarSjrnDsList",
                group="tourism",
                required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self,
            spec: SourceSpec,
            parameters: dict[str, object],
            *,
            include_empty: bool,
        ):
            yield ApiPage(
                rows=[{"baseYm": "202601", "signguNm": "사하구", "unknown": "2"}],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=b'{"data":[{"unknown":"2"}]}',
                schema_fingerprint="unexpected-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    result = load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert result.sources_ready == ()
    assert db.query(
        "select status from source_status order by checked_at desc limit 1"
    ) == [("SCHEMA_CHANGED",)]
    assert db.query("select count(*) from raw_artifact") == [(1,)]


def test_missing_service_key_is_reported_as_authentication_unavailable(
    tmp_path: Path,
) -> None:
    """A missing credential is not an unresolved portal contract."""
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="area_tourism_demand",
                url="https://example.test/AreaTarDemDsService",
                operation="areaTarSjrnDsList",
                group="tourism",
                required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            ),
        )
    )

    load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert db.query("select status from source_status") == [("AUTH_FAILED",)]


def test_unreviewed_operation_name_is_not_collected(
    tmp_path: Path, monkeypatch
) -> None:
    """A typo or guessed operation must remain unresolved before any network call."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="area_tourism_demand",
                url="https://example.test/AreaTarDemDsService",
                operation="guessedOperation",
                group="tourism",
                required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            ),
        )
    )
    monkeypatch.setattr(
        "westbusan.demand.load.DataGoKrPager",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("unreviewed operation called")
        ),
    )

    result = load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert result.artifacts_written == 0
    assert db.query("select status from source_status") == [("SPEC_UNRESOLVED",)]


def test_explicit_empty_page_is_retained_for_historical_backfill(
    tmp_path: Path, monkeypatch
) -> None:
    """Dropping an empty response makes the two-empty-years stop rule unverifiable."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="area_tourism_demand",
                url="https://example.test/AreaTarDemDsService",
                operation="areaTarSjrnDsList",
                group="tourism",
                required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self,
            spec: SourceSpec,
            parameters: dict[str, object],
            *,
            include_empty: bool,
        ):
            assert include_empty is True
            yield ApiPage(
                rows=[],
                total_count=0,
                page_no=1,
                page_size=1,
                raw_body=b'{"data":[]}',
                schema_fingerprint="empty-schema",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    result = load_tourism_demand(
        db,
        registry,
        date(2022, 1, 1),
        date(2022, 1, 31),
        RunContext(uuid4(), "backfill", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert result.artifacts_written == 1
    assert db.query("select status from source_status order by checked_at desc") == [
        ("EMPTY",)
    ]
    assert db.query("select source_date from raw_artifact") == [(date(2022, 1, 1),)]

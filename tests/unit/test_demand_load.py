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
    assert record.metric_code == "area_tar_sjrn_ds_list.2102"
    assert record.unit == "ratio"
    assert record.metric_value == 0.31


def test_resource_demand_keeps_provider_submetric_identity() -> None:
    """Treating consumption as visitors would corrupt its source-native grain."""
    row = json.loads(
        (Path("tests/fixtures/demand/area_consumption.json")).read_text(
            encoding="utf-8"
        )
    )[0]
    record = normalize_demand_row("area_tourism_consumption", row)

    assert record.metric_code == "area_tar_svc_dem_list.1101"
    assert record.unit == "SNS mentions"
    assert record.metric_value == 430000


def test_datalab_filters_non_busan_duplicate_district_names_without_schema_change(
    tmp_path: Path, monkeypatch
) -> None:
    """The nationwide DataLab feed must use the jurisdiction code, not a duplicate name."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="tourism_data_lab",
                url="https://example.test/DataLabService",
                operation="locgoRegnVisitrDDList",
                group="tourism",
                required_parameters={"startYmd": "{startYmd}", "endYmd": "{endYmd}"},
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            rows = [
                {
                    "baseYmd": "20260115",
                    "signguCode": "26320",
                    "daywkDivCd": "1",
                    "signguNm": "북구",
                    "touDivCd": "1",
                    "touDivNm": "외지인",
                    "touNum": "1200",
                    "daywkDivNm": "월",
                },
                {
                    "baseYmd": "20260115",
                    "signguCode": "27200",
                    "daywkDivCd": "1",
                    "signguNm": "북구",
                    "touDivCd": "1",
                    "touDivNm": "외지인",
                    "touNum": "9800",
                    "daywkDivNm": "월",
                },
                {
                    "baseYmd": "20260115",
                    "signguCode": "27110",
                    "daywkDivCd": "1",
                    "signguNm": "중구",
                    "touDivCd": "1",
                    "touDivNm": "외지인",
                    "touNum": "9700",
                    "daywkDivNm": "월",
                },
            ]
            yield ApiPage(
                rows=rows,
                total_count=len(rows),
                page_no=1,
                page_size=len(rows),
                raw_body=json.dumps({"data": rows}, ensure_ascii=False).encode(),
                schema_fingerprint="locgo-regn-visitr-fields",
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
    assert result.sources_ready == ("tourism_data_lab",)
    assert db.query("select district, region_group, metric_value from fact_tourism_demand") == [
        ("북구", "west", 1200.0)
    ]
    assert db.query("select status from source_status") == [("READY",)]


def test_datalab_missing_jurisdiction_code_is_schema_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    """A name cannot substitute for the documented DataLab jurisdiction code."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="tourism_data_lab",
                url="https://example.test/DataLabService",
                operation="locgoRegnVisitrDDList",
                group="tourism",
                required_parameters={"startYmd": "{startYmd}", "endYmd": "{endYmd}"},
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            row = {
                "baseYmd": "20260115",
                "signguNm": "중구",
                "touNum": "1200",
            }
            yield ApiPage(
                rows=[row],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=json.dumps({"data": [row]}, ensure_ascii=False).encode(),
                schema_fingerprint="missing-jurisdiction-code",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    result = load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert result.sources_skipped == ("tourism_data_lab",)
    assert db.query("select status from source_status") == [("SCHEMA_CHANGED",)]


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
                        "tarSjrnDsIxVal": "0.31",
                        "tarSjrnDsIxCd": "2102",
                        "tarSjrnDsIxNm": "숙박 비중",
                    }
                ],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=(
                    '{"data":[{"baseYm":"202601","signguNm":"사하구","tarSjrnDsIxVal":"0.31","tarSjrnDsIxCd":"2102","tarSjrnDsIxNm":"숙박 비중"}]}'
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
    ) == [
        ("area_tar_sjrn_ds_list.2102", "2026-01", "사하구", "west", 0.31)
    ]
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
                        "tarSjrnDsIxVal": "0.31",
                        "tarSjrnDsIxCd": "2102",
                        "tarSjrnDsIxNm": "숙박 비중",
                    }
                ],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=json.dumps(
                    {
                        "data": [
                            {
                                "baseYm": "202601",
                                "tarSjrnDsIxVal": "0.31",
                                "tarSjrnDsIxCd": "2102",
                                "tarSjrnDsIxNm": "숙박 비중",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode(),
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
        ("area_tar_sjrn_ds_list.2102", "ratio")
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


def test_loader_collects_each_reviewed_operation_for_one_source(
    tmp_path: Path, monkeypatch
) -> None:
    """Keeping only the newest inspection would silently drop a required subseries."""
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
    for operation in ("areaTarSjrnDsList", "areaTarExpDsList"):
        record_inspection(
            unresolved,
            db,
            operation=operation,
            required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            response_row_path="response.body.items.item",
            portal_detail_url="https://www.data.go.kr/data/15151868/openapi.do",
        )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            row = (
                {
                    "baseYm": "202601",
                    "signguNm": "사하구",
                    "tarSjrnDsIxVal": "0.31",
                    "tarSjrnDsIxCd": "2102",
                    "tarSjrnDsIxNm": "숙박 비중",
                }
                if spec.url.endswith("/areaTarSjrnDsList")
                else {
                    "baseYm": "202601",
                    "signguNm": "사하구",
                    "tarExpDsIxVal": "175000",
                    "tarExpDsIxCd": "2203",
                    "tarExpDsIxNm": "방문량 대비 방문 소비액",
                }
            )
            yield ApiPage(
                rows=[row],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=json.dumps({"data": [row]}, ensure_ascii=False).encode(),
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

    assert result.records_loaded == 2
    assert sorted(db.query("select metric_code from fact_tourism_demand")) == [
        ("area_tar_exp_ds_list.2203",),
        ("area_tar_sjrn_ds_list.2102",),
    ]


def test_concentration_uses_reviewed_area_parameters_and_returned_base_ymd(
    tmp_path: Path, monkeypatch
) -> None:
    """Concentration is a current area call; ``baseYmd`` exists only in its rows."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="tourism_concentration_rate",
                url="https://example.test/TatsCnctrRateService",
                operation="tatsCnctrRatedList",
                group="tourism",
                required_parameters={
                    "areaCd": "26",
                    "signguCd": "26380",
                },
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            assert spec.url.endswith("/tatsCnctrRatedList")
            assert parameters == {
                "areaCd": "26",
                "signguCd": "26380",
            }
            row = {
                "baseYmd": "20260131",
                "signguNm": "사하구",
                "cnctrRate": "42.5",
                "tAtsNm": "다대포해수욕장",
            }
            yield ApiPage(
                rows=[row],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=json.dumps({"data": [row]}, ensure_ascii=False).encode(),
                schema_fingerprint="tats-cnctr-rate-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    load_tourism_demand(
        db,
        registry,
        date(2026, 1, 1),
        date(2026, 1, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert db.query("select metric_code, unit, period from fact_tourism_demand") == [
        ("tats_cnctr_rated_list.visitor_concentration_rate", "percent", "2026-01-31")
    ]


def test_concentration_still_issues_one_current_call_when_the_requested_dates_are_future(
    tmp_path: Path, monkeypatch
) -> None:
    """The current bounded profile has no request date and must not be filtered as history."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="tourism_concentration_rate",
                url="https://example.test/TatsCnctrRateService",
                operation="tatsCnctrRatedList",
                group="tourism",
                required_parameters={"areaCd": "26", "signguCd": "26380"},
            ),
        )
    )
    calls = 0

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            nonlocal calls
            calls += 1
            assert parameters == {"areaCd": "26", "signguCd": "26380"}
            yield ApiPage(
                rows=[],
                total_count=0,
                page_no=1,
                page_size=1,
                raw_body=b'{"data":[]}',
                schema_fingerprint="tats-cnctr-rate-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    load_tourism_demand(
        db,
        registry,
        date(2026, 12, 1),
        date(2026, 12, 31),
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert calls == 1


def test_mixed_rows_from_a_different_reviewed_operation_are_schema_changed(
    tmp_path: Path, monkeypatch
) -> None:
    """A page may be READY only when every nonempty row fits its selected operation."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry(
        (
            SourceSpec(
                source_id="area_tourism_demand",
                url="https://example.test/AreaTarDemDsService",
                operation="areaTarExpDsList",
                group="tourism",
                required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            ),
        )
    )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            rows = [
                {
                    "baseYm": "202601",
                    "signguNm": "사하구",
                    "tarExpDsIxVal": "175000",
                    "tarExpDsIxCd": "2203",
                    "tarExpDsIxNm": "방문량 대비 방문 소비액",
                },
                {
                    "baseYm": "202601",
                    "signguNm": "사하구",
                    "tarSjrnDsIxVal": "0.31",
                    "tarSjrnDsIxCd": "2102",
                    "tarSjrnDsIxNm": "숙박 비중",
                },
            ]
            yield ApiPage(
                rows=rows,
                total_count=2,
                page_no=1,
                page_size=2,
                raw_body=json.dumps({"data": rows}, ensure_ascii=False).encode(),
                schema_fingerprint="mixed-operation-fields",
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
    assert db.query("select status from source_status") == [("SCHEMA_CHANGED",)]
    assert db.query("select metric_code from fact_tourism_demand") == [
        ("area_tar_exp_ds_list.2203",)
    ]


def test_backfill_starts_at_2022_and_persists_two_empty_year_stop_state(
    tmp_path: Path, monkeypatch
) -> None:
    """Backfill state prevents silently skipping 2022 or probing empty history forever."""
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
    calls: list[str] = []

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            calls.append(str(parameters["baseYm"]))
            yield ApiPage(
                rows=[],
                total_count=0,
                page_no=1,
                page_size=1,
                raw_body=b'{"data":[]}',
                schema_fingerprint="empty-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    run = RunContext(uuid4(), "backfill", datetime(2026, 2, 1, tzinfo=UTC))

    load_tourism_demand(db, registry, date(2022, 12, 1), date(2022, 12, 31), run)

    assert calls == [f"2022{month:02d}" for month in range(1, 13)]
    checkpoint = json.loads(
        db.query(
            "select checkpoint_json from collection_checkpoint where source_id = ?",
            ["area_tourism_demand"],
        )[0][0]
    )
    assert checkpoint["phase"] == "older_yearly"
    assert checkpoint["next_year"] == 2021
    assert checkpoint["consecutive_explicitly_empty_years"] == 1

    calls.clear()
    load_tourism_demand(db, registry, date(2022, 12, 1), date(2022, 12, 31), run)
    assert calls == [f"2021{month:02d}" for month in range(1, 13)]
    checkpoint = json.loads(
        db.query(
            "select checkpoint_json from collection_checkpoint where source_id = ?",
            ["area_tourism_demand"],
        )[0][0]
    )
    assert checkpoint["phase"] == "stopped_after_two_empty_years"

    calls.clear()
    load_tourism_demand(db, registry, date(2022, 12, 1), date(2022, 12, 31), run)
    assert calls == []


@pytest.mark.parametrize(
    "source_id,start,end,required_parameters",
    [
        (
            "tourism_data_lab",
            date(2026, 1, 1),
            date(2026, 1, 31),
            {"startYmd": "{startYmd}", "endYmd": "{endYmd}"},
        ),
        (
            "area_tourism_demand",
            date(2026, 1, 1),
            date(2026, 1, 31),
            {"baseYm": "{baseYm}"},
        ),
        (
            "area_tourism_consumption",
            date(2026, 1, 1),
            date(2026, 1, 31),
            {"baseYm": "{baseYm}"},
        ),
        (
            "tourism_concentration_rate",
            date(2026, 1, 1),
            date(2026, 1, 31),
            {"areaCd": "26", "signguCd": "26380"},
        ),
        (
            "area_tourism_destination_division",
            date(2026, 1, 1),
            date(2026, 1, 31),
            {"baseYm": "{baseYm}"},
        ),
        (
            "related_tourism_destinations",
            date(2025, 3, 1),
            date(2025, 3, 31),
            {"baseYm": "{baseYm}"},
        ),
    ],
)
def test_loader_collects_every_documented_operation_from_reviewed_inspections(
    tmp_path: Path,
    monkeypatch,
    source_id: str,
    start: date,
    end: date,
    required_parameters: dict[str, str],
) -> None:
    """Every KTO operation must use its recorded contract and preserve its own rows."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    source = SourceSpec(
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        group="tourism",
        inspection_required=True,
    )
    registry = SourceRegistry((source,))
    official_rows = [
        item
        for item in json.loads(
            Path("tests/fixtures/demand/official_rows.json").read_text(encoding="utf-8")
        )
        if item["source_id"] == source_id
    ]
    rows_by_operation: dict[str, list[dict[str, object]]] = {}
    for item in official_rows:
        operation = item["operation"]
        rows_by_operation.setdefault(operation, []).append(item["row"])
    for operation in rows_by_operation:
        record_inspection(
            source,
            db,
            operation=operation,
            required_parameters=required_parameters,
            response_row_path="response.body.items.item",
            portal_detail_url="https://www.data.go.kr/",
        )

    called_operations: list[str] = []

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            expected_parameters = (
                {"startYmd": "20260101", "endYmd": "20260131"}
                if source_id == "tourism_data_lab"
                else {"areaCd": "26", "signguCd": "26380"}
                if source_id == "tourism_concentration_rate"
                else {"baseYm": f"{start.year:04d}{start.month:02d}"}
            )
            assert parameters == expected_parameters
            operation = spec.url.rsplit("/", maxsplit=1)[-1]
            called_operations.append(operation)
            yield ApiPage(
                rows=rows_by_operation[operation],
                total_count=len(rows_by_operation[operation]),
                page_no=1,
                page_size=len(rows_by_operation[operation]),
                raw_body=json.dumps(
                    {"data": rows_by_operation[operation]}, ensure_ascii=False
                ).encode(),
                schema_fingerprint=f"{operation}-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    result = load_tourism_demand(
        db,
        registry,
        start,
        end,
        RunContext(uuid4(), "test", datetime(2026, 2, 1, tzinfo=UTC)),
    )

    assert result.records_loaded == len(official_rows)
    assert set(called_operations) == set(rows_by_operation)
    assert set(db.query("select metric_code, unit from fact_tourism_demand")) == {
        (item["metric_code"], item["unit"]) for item in official_rows
    }


def test_source_is_not_ready_when_any_required_reviewed_operation_is_schema_changed(
    tmp_path: Path, monkeypatch
) -> None:
    """One successful subseries cannot conceal a failed required operation."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-service-key")
    db = Database(tmp_path / "tourism.duckdb", Path("sql"))
    db.migrate()
    source = SourceSpec(
        source_id="area_tourism_demand",
        url="https://example.test/AreaTarDemDsService",
        group="tourism",
        inspection_required=True,
    )
    registry = SourceRegistry((source,))
    for operation in ("areaTarSjrnDsList", "areaTarExpDsList"):
        record_inspection(
            source,
            db,
            operation=operation,
            required_parameters={"baseYm": "{baseYm}", "areaCd": "26"},
            response_row_path="response.body.items.item",
            portal_detail_url="https://www.data.go.kr/data/15151868/openapi.do",
        )

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            row = (
                {
                    "baseYm": "202601",
                    "signguNm": "사하구",
                    "tarSjrnDsIxVal": "0.31",
                    "tarSjrnDsIxCd": "2102",
                    "tarSjrnDsIxNm": "숙박 비중",
                }
                if spec.url.endswith("/areaTarSjrnDsList")
                else {"baseYm": "202601", "signguNm": "사하구", "unknown": "2"}
            )
            yield ApiPage(
                rows=[row],
                total_count=1,
                page_no=1,
                page_size=1,
                raw_body=json.dumps({"data": [row]}, ensure_ascii=False).encode(),
                schema_fingerprint="reviewed-operation-fields",
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
    assert result.sources_ready == ()
    assert result.sources_skipped == ("area_tourism_demand",)


def test_backfill_caps_future_end_at_the_injected_latest_complete_month(
    tmp_path: Path, monkeypatch
) -> None:
    """Future and in-progress months must not enter artifacts or checkpoint completion."""
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
    calls: list[str] = []

    class FakePager:
        def __init__(self, client: object, service_key: str) -> None:
            pass

        def iter_pages(
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            calls.append(str(parameters["baseYm"]))
            yield ApiPage(
                rows=[],
                total_count=0,
                page_no=1,
                page_size=1,
                raw_body=b'{"data":[]}',
                schema_fingerprint="empty-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    load_tourism_demand(
        db,
        registry,
        date(2025, 1, 1),
        date(2026, 12, 31),
        RunContext(uuid4(), "backfill", datetime(2026, 2, 15, tzinfo=UTC)),
    )

    assert calls[0] == "202201"
    assert calls[-1] == "202601"
    checkpoint = json.loads(
        db.query("select checkpoint_json from collection_checkpoint")[0][0]
    )
    assert checkpoint["initial_complete_through"] == "2026-01"


def test_partial_empty_initial_year_does_not_count_toward_the_two_year_stop(
    tmp_path: Path, monkeypatch
) -> None:
    """An explicit empty year requires all twelve monthly observations."""
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
            self, spec: SourceSpec, parameters: dict[str, object], *, include_empty: bool
        ):
            yield ApiPage(
                rows=[],
                total_count=0,
                page_no=1,
                page_size=1,
                raw_body=b'{"data":[]}',
                schema_fingerprint="empty-fields",
            )

    monkeypatch.setattr("westbusan.demand.load.DataGoKrPager", FakePager)
    load_tourism_demand(
        db,
        registry,
        date(2022, 1, 1),
        date(2022, 1, 31),
        RunContext(uuid4(), "backfill", datetime(2022, 2, 1, tzinfo=UTC)),
    )

    checkpoint = json.loads(
        db.query("select checkpoint_json from collection_checkpoint")[0][0]
    )
    assert checkpoint["consecutive_explicitly_empty_years"] == 0

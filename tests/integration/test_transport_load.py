import json
from datetime import UTC, datetime
from pathlib import Path

from westbusan.db import Database
from westbusan.http import HttpResult
from westbusan.models import RunContext
from westbusan.sources.registry import SourceRegistry, record_inspection
from westbusan.transport.load import (
    TransportMeasure,
    load_transport,
    normalize_transport_row,
    normalize_transport_rows,
)


def test_normalize_metro_row_keeps_unmapped_station_out_of_a_district() -> None:
    row = json.loads(
        Path("tests/fixtures/transport/metro_rows.json").read_text(encoding="utf-8")
    )[1]

    record = normalize_transport_row("busan_metro_odcloud_discovery", row)

    assert record.period == "2024-07-31"
    assert record.station == "검토필요역"
    assert record.district == "UNMAPPED"
    assert record.region_group == "unresolved"
    assert record.boarding is None
    assert record.alighting == 21
    assert record.measures == (
        TransportMeasure("metro_alighting", 21, "passengers", {"scope": "total"}),
        TransportMeasure(
            "metro_alighting",
            5,
            "passengers",
            {"hour_band": "08시-09시", "scope": "hourly"},
        ),
    )


def test_normalize_official_metro_wide_row_keeps_line_gate_and_hour_scopes() -> None:
    row = json.loads(
        Path("tests/fixtures/transport/metro_rows.json").read_text(encoding="utf-8")
    )[0]

    record = normalize_transport_row("busan_metro_odcloud_discovery", row)

    assert record.period == "2024-07-31"
    assert record.station == "사상역"
    assert record.boarding == 1250
    assert record.alighting is None
    assert record.dimensions["station_number"] == 227
    assert record.dimensions["line"] == "2호선"
    assert record.dimensions["게이트구분"] == "정문"
    assert [measure.value for measure in record.measures] == [1250, 210, 95]


def test_normalize_official_od_row_keeps_all_location_codes_without_inventing_mode() -> None:
    row = json.loads(Path("tests/fixtures/transport/od_row.json").read_text(encoding="utf-8"))

    record = normalize_transport_row("public_transport_od_usage", row)

    assert record.period == "2024-07"
    assert record.origin == "부산광역시 사상구 괘법동"
    assert record.destination == "부산광역시 사하구 하단동"
    assert record.mode == "시내버스-도시철도"
    assert record.count == 347
    assert record.district == "사하구"
    assert record.dimensions["dptre_emd_cd"] == "2653010900"
    assert record.dimensions["arvl_emd_cd"] == "2638010200"


def test_normalize_srt_keeps_boarding_and_alighting_as_separate_native_measures() -> None:
    record = normalize_transport_row(
        "srt_station_boarding_file",
        {"집계년도": 2024, "집계월": 8, "역명": "부산역", "승차인원": 120, "하차인원": 110, "비고": "잠정"},
    )

    assert [(measure.metric_code, measure.value) for measure in record.measures] == [
        ("srt_boarding", 120),
        ("srt_alighting", 110),
    ]
    assert record.dimensions["비고"] == "잠정"
    assert "승차인원" not in record.dimensions
    assert "하차인원" not in record.dimensions


def test_normalize_wide_srt_boarding_unpivots_month_columns_without_inventing_alighting() -> None:
    records = normalize_transport_rows(
        "srt_station_boarding_file",
        {"승차역": "부산역", "2019년1월": 30, "2019년2월": 40, "권역": "부산"},
    )

    assert [record.period for record in records] == ["2019-01", "2019-02"]
    assert all(record.alighting is None for record in records)
    assert [(measure.metric_code, measure.value) for measure in records[0].measures] == [
        ("srt_boarding", 30)
    ]
    assert records[0].dimensions["권역"] == "부산"


def test_korail_survey_uses_only_reviewed_measure_fields_and_fixed_context_period() -> None:
    record = normalize_transport_row(
        "korail_workplace_ticketing_file",
        {
            "고객등급": "일반",
            "권종": "일반",
            "열차종": "KTX",
            "발매구분": "창구",
            "근무지시도코드": 26,
            "근무지시군구코드": 26530,
            "근무지시군구명": "사상구",
            "차종": "승용",
            "이용인원": 100,
        },
    )

    assert record.period == "2022-04..2022-06"
    assert [(measure.metric_code, measure.value, measure.unit) for measure in record.measures] == [
        ("korail_workplace_passenger_count", 100, "passengers")
    ]
    assert record.dimensions["근무지시군구코드"] == 26530
    assert record.dimensions["고객등급"] == "일반"


def test_load_transport_registers_static_korail_file_at_native_grain(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True)
    target = inbox / "KORAIL_근무지_2022.csv"
    target.write_bytes(Path("tests/fixtures/transport/railway.csv").read_bytes())
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()
    run = RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC))

    result = load_transport(db, SourceRegistry.load(Path("config/sources.yaml")), run)

    assert result.records_loaded == 2
    assert result.artifacts_written == 1
    assert result.sources_ready == ("korail_workplace_ticketing_file",)
    assert db.query(
        "select period, metric_code, unit, source_revision from fact_transport_flow order by district"
    ) == [
        ("2022-04..2022-06", "korail_workplace_passenger_count", "passengers", db.query("select content_hash from raw_artifact")[0][0]),
        ("2022-04..2022-06", "korail_workplace_passenger_count", "passengers", db.query("select content_hash from raw_artifact")[0][0]),
    ]
    assert db.query("select source_date from raw_artifact") == [(None,)]


def test_repeated_file_hash_is_auditable_without_duplicate_transport_facts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "KORAIL_근무지_2022.csv").write_bytes(
        Path("tests/fixtures/transport/railway.csv").read_bytes()
    )
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry.load(Path("config/sources.yaml"))

    load_transport(db, registry, RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)))
    result = load_transport(db, registry, RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)))

    assert result.records_loaded == 0
    assert db.query("select count(*) from raw_artifact") == [(2,)]
    assert db.query("select count(*) from fact_transport_flow") == [(2,)]


def test_unparseable_railway_rows_are_not_marked_ready(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "한국철도공사_근무지_2022.csv").write_text("역명,월\n부산역,202208\n", encoding="utf-8")
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()

    result = load_transport(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)),
    )

    assert "korail_workplace_ticketing_file" not in result.sources_ready
    assert db.query(
        "select status from source_status where source_id = 'korail_workplace_ticketing_file'"
    ) == [("SCHEMA_CHANGED",)]


def test_live_collectors_store_odcloud_and_data_go_pages_before_transport_facts(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()
    registry = SourceRegistry.load(Path("config/sources.yaml"))
    record_inspection(
        registry.get("public_transport_od_usage"),
        db,
        operation="getODUsage",
        required_parameters={"opr_ym": "202407"},
        response_row_path="data",
        portal_detail_url="https://www.data.go.kr/data/example",
    )
    monkeypatch.setenv("WESTBUSAN_ENABLE_LIVE_TRANSPORT", "true")
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "data-go-secret")
    monkeypatch.setenv("ODCLOUD_API_KEY", "odcloud-secret")
    swagger = Path("tests/fixtures/odcloud/swagger.json").read_bytes()
    metro_row = json.loads(
        Path("tests/fixtures/transport/metro_rows.json").read_text(encoding="utf-8")
    )[0]
    od_row = json.loads(Path("tests/fixtures/transport/od_row.json").read_text(encoding="utf-8"))

    class FixtureClient:
        def get(self, url: str, params: dict[str, object]) -> HttpResult:
            if url == "https://infuser.odcloud.kr/oas/docs":
                return HttpResult(200, swagger, "application/json")
            if "/3057229/v1/uddi:99999999-9999-9999-9999-999999999999" in url:
                return HttpResult(
                    200,
                    json.dumps({"data": [metro_row], "totalCount": 1, "page": 1, "perPage": 1000}).encode(),
                    "application/json",
                )
            if url.endswith("/getODUsage"):
                return HttpResult(
                    200,
                    json.dumps({"data": [od_row], "totalCount": 1, "pageNo": 1, "numOfRows": 1000}).encode(),
                    "application/json",
                )
            raise AssertionError(url)

    result = load_transport(
        db,
        registry,
        RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)),
        client=FixtureClient(),
    )

    assert result.records_loaded == 2
    assert result.artifacts_written == 2
    assert result.sources_ready == (
        "public_transport_od_usage",
        "busan_metro_odcloud_discovery",
    )
    assert db.query("select count(*) from raw_artifact") == [(2,)]
    assert db.query("select count(*) from fact_transport_flow") == [(4,)]
    assert '"row_count":1' in db.query(
        "select request_json from raw_artifact where source_id = 'busan_metro_odcloud_discovery'"
    )[0][0]
    assert '"row_count": 1' in db.query(
        "select detail_json from source_status where source_id = 'busan_metro_odcloud_discovery' order by checked_at desc"
    )[0][0]
    assert db.query(
        "select distinct source_revision from fact_transport_flow where source_id = 'busan_metro_odcloud_discovery'"
    )[0][0].startswith("odcloud:99999999-9999-9999-9999-999999999999:")
    requests = db.query("select request_json from raw_artifact")
    assert all("data-go-secret" not in request[0] for request in requests)
    assert all("odcloud-secret" not in request[0] for request in requests)

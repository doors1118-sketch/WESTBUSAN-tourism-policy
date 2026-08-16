import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import westbusan.transport.load as transport_load_module
from westbusan.db import Database
from westbusan.http import HttpResult, SafeHttpClient
from westbusan.models import RunContext
from westbusan.sources.files import read_tabular_rows
from westbusan.sources.odcloud import build_odcloud_client, discover_latest_dataset
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
        read_tabular_rows(Path("tests/fixtures/transport/railway.csv"))[0],
    )

    assert record.period == "2022-04..2022-06"
    assert len(record.measures) == 20
    assert ("korail_workplace_smart_ticket_count", 100, "count") in [
        (measure.metric_code, measure.value, measure.unit) for measure in record.measures
    ]
    assert all(measure.unit == "count" for measure in record.measures)
    assert record.dimensions["시군구코드"] == "530"
    assert not any("시군구코드" in measure.metric_code for measure in record.measures)


def test_korail_residence_survey_preserves_legal_dong_codes_as_dimensions() -> None:
    record = normalize_transport_row(
        "korail_residence_ticketing_file",
        read_tabular_rows(Path("tests/fixtures/transport/railway_residence.csv"))[0],
    )

    assert record.period == "2022-04..2022-06"
    assert len(record.measures) == 20
    assert ("korail_residence_ktx_count", 42, "count") in [
        (measure.metric_code, measure.value, measure.unit) for measure in record.measures
    ]
    assert record.dimensions["법정동시도코드"] == "26"
    assert record.dimensions["법정동시군구코드"] == "380"
    assert record.dimensions["차량보유"] == "미보유"


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

    assert result.records_loaded == 1
    assert result.artifacts_written == 1
    assert result.sources_ready == ("korail_workplace_ticketing_file",)
    assert db.query("select count(*) from fact_transport_flow") == [(20,)]
    assert db.query(
        "select period, metric_code, metric_value, unit from fact_transport_flow where metric_code = 'korail_workplace_smart_ticket_count'"
    ) == [("2022-04..2022-06", "korail_workplace_smart_ticket_count", 100, "count")]
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
    assert db.query("select count(*) from fact_transport_flow") == [(20,)]


def test_load_transport_preserves_official_residence_codes_without_measure_leakage(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "한국철도공사_거주지별_2022.csv").write_bytes(
        Path("tests/fixtures/transport/railway_residence.csv").read_bytes()
    )
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()

    result = load_transport(
        db,
        SourceRegistry.load(Path("config/sources.yaml")),
        RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)),
    )

    assert result.records_loaded == 1
    assert result.sources_ready == ("korail_residence_ticketing_file",)
    assert db.query("select count(*) from fact_transport_flow") == [(20,)]
    assert db.query(
        "select metric_value, unit from fact_transport_flow where metric_code = 'korail_residence_ktx_count'"
    ) == [(42, "count")]
    assert '"법정동시군구코드":"380"' in db.query(
        "select dimension_json from fact_transport_flow limit 1"
    )[0][0]


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
    file_detail = Path("tests/fixtures/odcloud/file_detail.json").read_bytes()
    metro_row = json.loads(
        Path("tests/fixtures/transport/metro_rows.json").read_text(encoding="utf-8")
    )[0]
    od_row = json.loads(Path("tests/fixtures/transport/od_row.json").read_text(encoding="utf-8"))

    class FixtureClient:
        def get(self, url: str, params: dict[str, object]) -> HttpResult:
            if url == "https://infuser.odcloud.kr/oas/docs":
                return HttpResult(200, swagger, "application/json")
            if url == "https://www.data.go.kr/data/3057229/fileData.do":
                return HttpResult(
                    200,
                    (
                        b'<html><body><input id="publicDataDetailPk" '
                        b'value="uddi:99999999-9999-9999-9999-999999999999"></body></html>'
                    ),
                    "text/html",
                )
            if url == "https://www.data.go.kr/tcs/dss/selectFileDataDownload.do":
                assert params["publicDataPk"] == "3057229"
                return HttpResult(200, file_detail, "application/json")
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
    assert result.artifacts_written == 3
    assert result.sources_ready == (
        "public_transport_od_usage",
        "busan_metro_odcloud_discovery",
    )
    assert db.query("select count(*) from raw_artifact") == [(3,)]
    assert db.query("select count(*) from fact_transport_flow") == [(4,)]
    assert '"row_count":1' in db.query(
        "select request_json from raw_artifact where source_id = 'busan_metro_odcloud_discovery' and request_json like '%\"page\":1%'"
    )[0][0]
    assert '"row_count": 1' in db.query(
        "select detail_json from source_status where source_id = 'busan_metro_odcloud_discovery' order by checked_at desc"
    )[0][0]
    detail = json.loads(db.query(
        "select detail_json from source_status where source_id = 'busan_metro_odcloud_discovery' order by checked_at desc"
    )[0][0])
    assert detail["publication_date"] == "2026-07-22"
    assert detail["registered_at_provenance"] == "data_go_file_detail.registDt"
    assert detail["modified_at_provenance"] == "data_go_file_detail.updtDt"
    assert db.query(
        "select distinct source_revision from fact_transport_flow where source_id = 'busan_metro_odcloud_discovery'"
    )[0][0].startswith("odcloud:99999999-9999-9999-9999-999999999999:")
    requests = db.query("select request_json from raw_artifact")
    assert all("data-go-secret" not in request[0] for request in requests)
    assert all("odcloud-secret" not in request[0] for request in requests)


def test_live_odcloud_collection_scopes_credential_to_dataset_host(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    db = Database(data_dir / "test.duckdb", Path("sql"))
    db.migrate()
    spec = SourceRegistry.load(Path("config/sources.yaml")).get(
        "busan_metro_odcloud_discovery"
    )
    registry = SourceRegistry((spec,))
    swagger = Path("tests/fixtures/odcloud/swagger.json").read_bytes()
    file_detail = Path("tests/fixtures/odcloud/file_detail.json").read_bytes()
    metro_row = json.loads(
        Path("tests/fixtures/transport/metro_rows.json").read_text(encoding="utf-8")
    )[0]
    outgoing: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        outgoing.append((request.url.host, request.headers.get("Authorization")))
        if request.url.host == "infuser.odcloud.kr":
            return httpx.Response(200, content=swagger)
        if request.url.host == "www.data.go.kr" and request.url.path.endswith("fileData.do"):
            return httpx.Response(
                200,
                content=(
                    b'<html><body><input id="publicDataDetailPk" '
                    b'value="uddi:99999999-9999-9999-9999-999999999999"></body></html>'
                ),
                headers={"content-type": "text/html"},
            )
        if request.url.host == "www.data.go.kr":
            return httpx.Response(200, content=file_detail)
        if request.url.host == "api.odcloud.kr":
            return httpx.Response(
                200,
                json={"data": [metro_row], "totalCount": 1, "page": 1, "perPage": 1000},
            )
        raise AssertionError(request.url)

    transport = httpx.MockTransport(handler)
    metadata_client = SafeHttpClient(httpx.Client(transport=transport), sleeper=lambda _: None)
    dataset_client = build_odcloud_client("odcloud-secret", transport=transport)
    monkeypatch.setattr(
        transport_load_module,
        "build_odcloud_metadata_client",
        lambda: metadata_client,
        raising=False,
    )
    monkeypatch.setattr(
        transport_load_module, "build_odcloud_client", lambda _: dataset_client
    )
    monkeypatch.setenv("WESTBUSAN_ENABLE_LIVE_TRANSPORT", "true")
    monkeypatch.setenv("ODCLOUD_API_KEY", "odcloud-secret")

    result = load_transport(
        db,
        registry,
        RunContext.start("backfill", datetime(2026, 8, 16, tzinfo=UTC)),
    )

    assert result.records_loaded == 1
    assert [authorization for host, authorization in outgoing if host == "api.odcloud.kr"] == [
        "odcloud-secret"
    ]
    assert all(
        authorization is None
        for host, authorization in outgoing
        if host in {"infuser.odcloud.kr", "www.data.go.kr"}
    )


@pytest.mark.integration
def test_live_odcloud_file_detail_has_publication_metadata_when_opted_in() -> None:
    if os.getenv("WESTBUSAN_RUN_LIVE_CHECKS") != "1":
        pytest.skip("set WESTBUSAN_RUN_LIVE_CHECKS=1 to contact data.go.kr")

    revision = discover_latest_dataset(
        "3057229/v1",
        SafeHttpClient(),
        portal_detail_url="https://www.data.go.kr/data/3057229/fileData.do",
    )

    assert revision.registered_at is not None
    assert revision.published_at is not None
    assert revision.metadata["publication_provenance"] == "data_go_file_detail.updtDt"

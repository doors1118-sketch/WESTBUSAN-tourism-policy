from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from westbusan.db import Database
from westbusan.http import SafeHttpClient
from westbusan.sources.registry import (
    SourceRegistry,
    inspection_command,
    probe_source,
    record_inspection,
)


def test_registry_contains_all_accommodation_sources() -> None:
    registry = SourceRegistry.load(Path("config/sources.yaml"))
    assert set(registry.ids(group="accommodation")) == {
        "lodgings",
        "tourist_accommodations",
        "foreigner_city_homestays",
        "rural_homestays",
        "hanok_experience",
        "tourist_pensions",
    }
    assert registry.get("lodgings").operation == "info"
    assert registry.get("tourist_pensions").additive_facility is False


def test_every_accommodation_source_is_scoped_to_busan_current_stock() -> None:
    """Catches a nationwide current-state API being treated as Busan history."""
    registry = SourceRegistry.load(Path("config/sources.yaml"))
    expected_authority_codes = (
        "3250000",
        "3260000",
        "3270000",
        "3280000",
        "3290000",
        "3300000",
        "3310000",
        "3320000",
        "3330000",
        "3340000",
        "3350000",
        "3360000",
        "3370000",
        "3380000",
        "3390000",
        "3400000",
    )

    for source_id in registry.ids(group="accommodation"):
        source = registry.get(source_id)
        assert source.required_parameters == {}
        assert getattr(source, "parameter_partitions", None) == {
            "cond[OPN_ATMY_GRP_CD::EQ]": expected_authority_codes
        }
        assert source.temporal_semantics == "current_snapshot_only"


def test_registry_rejects_an_incomplete_accommodation_authority_partition(
    tmp_path: Path,
) -> None:
    """Catches a configuration edit silently omitting one Busan district."""
    path = tmp_path / "sources.yaml"
    path.write_text(
        """sources:
  - source_id: lodgings
    url: https://apis.data.go.kr/1741000/lodgings
    operation: info
    group: accommodation
    parameter_partitions:
      cond[OPN_ATMY_GRP_CD::EQ]: [3250000]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exact 16-code Busan authority partition"):
        SourceRegistry.load(path)


def test_registry_configures_odcloud_file_detail_profile() -> None:
    source = SourceRegistry.load(Path("config/sources.yaml")).get(
        "busan_metro_odcloud_discovery"
    )

    assert source.portal_detail_url == "https://www.data.go.kr/data/3057229/fileData.do"


def test_registry_pins_vworld_and_regeneration_assessment_contracts() -> None:
    registry = SourceRegistry.load(Path("config/sources.yaml"))

    for source_id in ("vworld_building_geometry", "vworld_land_use"):
        source = registry.get(source_id)
        assert source.url == "https://api.vworld.kr/req/data"
        assert source.cadence == "daily"
        assert source.raw_cache_reuse == "daily"
        assert source.credential_env == "VWORLD_API_KEY"

    regeneration = registry.get("urban_regeneration_snapshot")
    assert regeneration.transport == "file_snapshot"
    assert regeneration.endpoint_url is None


def test_registry_pins_server_only_vworld_address_geocode_contract() -> None:
    """Catches accommodation geocoding bypassing the reviewed credential boundary."""
    source = SourceRegistry.load(Path("config/sources.yaml")).get(
        "vworld_address_geocode"
    )

    assert source.url == "https://api.vworld.kr/req/address"
    assert source.cadence == "daily"
    assert source.raw_cache_reuse == "daily"
    assert source.credential_env == "VWORLD_API_KEY"


def test_registry_pins_official_kto_poi_contract() -> None:
    """Catches public POI layers drifting to an unreviewed or client-side source."""
    source = SourceRegistry.load(Path("config/sources.yaml")).get("tourism_poi_area")

    assert source.url == "https://apis.data.go.kr/B551011/KorService2"
    assert source.operation == "areaBasedList2"
    assert source.cadence == "monthly"
    assert source.page_size == 1000
    assert source.credential_env == "KTO_SERVICE_KEY"
    assert source.required_parameters == {
        "areaCode": "6",
        "MobileOS": "ETC",
        "MobileApp": "WestBusanPolicy",
    }


def test_registry_loads_source_metadata_from_fixture() -> None:
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    source = registry.get("ready_source")
    assert source.endpoint_url == "https://example.test/service/info"
    assert source.cadence == "daily"
    assert source.response_row_path == "data"


def test_accommodation_probe_is_scoped_to_a_reviewed_authority_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the readiness probe calling the nationwide endpoint without a Busan filter."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    source = SourceRegistry.load(Path("config/sources.yaml")).get("lodgings")
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(
            200,
            json={"data": [], "totalCount": 0, "pageNo": 1, "numOfRows": 1},
        )

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None
    )

    status = probe_source(source, client, db)

    assert status.status == "EMPTY"
    assert captured["cond[OPN_ATMY_GRP_CD::EQ]"] == "3250000"


def test_registry_rejects_unknown_source_id() -> None:
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    with pytest.raises(KeyError, match="unknown source_id: missing"):
        registry.get("missing")


def test_probe_records_ready_status_without_service_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(
            200,
            json={"data": [{"id": "one"}], "totalCount": 1},
            headers={"etag": '"probe-version"'},
        )

    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None
    )

    status = probe_source(registry.get("ready_source"), client, db)

    assert status.status == "READY"
    assert captured["pageNo"] == "1"
    assert captured["numOfRows"] == "1"
    saved_detail = db.query("select detail_json from source_status")[0][0]
    assert "test-service-key" not in saved_detail
    detail = json.loads(saved_detail)
    assert detail["endpoint"] == "https://example.test/service/info"
    assert detail["parameters"]["serviceKey"] == "[REDACTED]"
    assert detail["parameters"]["pageNo"] == 1
    assert detail["response"]["http_status"] == 200
    assert detail["response"]["headers"] == {
        "etag": '"probe-version"',
        "content-length": "38",
    }


def test_official_accommodation_probe_rejects_invented_paging_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches the one-row probe approving a malformed 1741000 response."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    spec = SourceRegistry.load(Path("config/sources.yaml")).get("lodgings")
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "data": [{"MNG_NO": "L1", "OPN_ATMY_GRP_CD": "6260000"}],
                        "totalCount": 1,
                    },
                )
            )
        ),
        sleeper=lambda _: None,
    )

    status = probe_source(spec, client, db)

    assert status.status == "SCHEMA_CHANGED"


def test_building_probe_accepts_official_success_with_empty_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a parcel-scoped building API success being mislabeled as schema drift."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    spec = SourceRegistry.load(Path("config/sources.yaml")).get(
        "building_register_title"
    )
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "response": {
                            "header": {
                                "resultCode": "00",
                                "resultMsg": "NORMAL SERVICE.",
                            },
                            "body": {},
                        }
                    },
                )
            )
        ),
        sleeper=lambda _: None,
    )

    status = probe_source(spec, client, db)

    assert status.status == "EMPTY"


def test_transport_probe_accepts_official_no_data_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpublished month is EMPTY, not a false schema-change incident."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    spec = SourceRegistry.load(Path("config/sources.yaml")).get(
        "public_transport_od_usage"
    )
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    record_inspection(
        spec,
        db,
        operation="getMonthlyODUsageforGeneralBusesandUrbanRailways",
        required_parameters={
            "opr_ym": "{baseYm}",
            "dptre_ctpv_cd": "26",
            "arvl_ctpv_cd": "26",
        },
        response_row_path="Response.body.items.item",
        portal_detail_url="https://www.data.go.kr/data/15142069/openapi.do",
    )
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"Error": {"code": "50", "message": "NO_DATA_FOUND"}},
                )
            )
        ),
        sleeper=lambda _: None,
    )

    status = probe_source(spec, client, db, probe_date=date(2026, 9, 4))

    assert status.status == "EMPTY"
    assert status.detail["parameters"]["opr_ym"] == "202608"
    assert status.detail["row_count"] == 0


@pytest.mark.parametrize(
    ("source_id", "body", "expected_status"),
    [
        ("ready_source", {"data": [], "totalCount": 0}, "EMPTY"),
        ("unresolved_source", None, "SPEC_UNRESOLVED"),
    ],
)
def test_probe_classifies_empty_and_unresolved_sources(
    tmp_path: Path,
    monkeypatch,
    source_id: str,
    body: dict[str, object] | None,
    expected_status: str,
) -> None:
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=body)

    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None
    )

    status = probe_source(registry.get(source_id), client, db)

    assert status.status == expected_status
    assert calls == (0 if expected_status == "SPEC_UNRESOLVED" else 1)


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        ({"response": {"header": {"resultCode": "20"}}}, "AUTH_FAILED"),
        ({"response": {"header": {"resultCode": "22"}}}, "QUOTA_EXCEEDED"),
        ({"unexpected": []}, "SCHEMA_CHANGED"),
    ],
)
def test_probe_classifies_portal_and_schema_errors(
    tmp_path: Path,
    monkeypatch,
    body: dict[str, object],
    expected_status: str,
) -> None:
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=json.dumps(body).encode())
            )
        ),
        sleeper=lambda _: None,
    )

    status = probe_source(registry.get("ready_source"), client, db)

    assert status.status == expected_status


@pytest.mark.parametrize(
    ("http_status", "expected_status"),
    [
        (401, "AUTH_FAILED"),
        (403, "AUTH_FAILED"),
        (429, "QUOTA_EXCEEDED"),
        (503, "HTTP_FAILED"),
    ],
)
def test_probe_classifies_conventional_http_statuses(
    tmp_path: Path, monkeypatch, http_status: int, expected_status: str
) -> None:
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(http_status, content=b"upstream failure")
            )
        ),
        sleeper=lambda _: None,
    )

    status = probe_source(registry.get("ready_source"), client, db)

    assert status.status == expected_status


def test_inspection_records_operation_details_before_probe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    inspected = record_inspection(
        registry.get("unresolved_source"),
        db,
        operation="selectedOperation",
        required_parameters={"baseYm": "202608"},
        response_row_path="response.body.items.item",
        portal_detail_url="https://data.go.kr/detail/example",
    )
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(
            200,
            json={
                "response": {
                    "body": {"items": {"item": [{"id": "one"}]}, "totalCount": 1}
                }
            },
        )

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
    )

    status = probe_source(registry.get("unresolved_source"), client, db)

    assert status.status == "READY"
    assert inspected.operation == "selectedOperation"
    assert captured["baseYm"] == "202608"
    detail = db.query(
        "select detail_json from source_status where source_id = ? order by checked_at",
        ["unresolved_source"],
    )[0][0]
    assert "https://data.go.kr/detail/example" in detail


def test_probe_resolves_reviewed_date_placeholders_to_latest_closed_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a live probe sending literal ``{baseYm}`` values to the provider."""
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    record_inspection(
        registry.get("unresolved_source"),
        db,
        operation="selectedOperation",
        required_parameters={
            "baseYm": "{baseYm}",
            "startYmd": "{startYmd}",
            "endYmd": "{endYmd}",
        },
        response_row_path="response.body.items.item",
        portal_detail_url="https://data.go.kr/detail/example",
    )
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(
            200,
            json={
                "response": {
                    "body": {"items": {"item": [{"id": "one"}]}, "totalCount": 1}
                }
            },
        )

    client = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleeper=lambda _: None
    )

    status = probe_source(
        registry.get("unresolved_source"), client, db, probe_date=date(2026, 8, 19)
    )

    assert status.status == "READY"
    assert captured["baseYm"] == "202607"
    assert captured["startYmd"] == "20260701"
    assert captured["endYmd"] == "20260731"


def test_probe_rejects_inspection_response_with_mismatched_row_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "test-service-key")
    registry = SourceRegistry.load(Path("tests/fixtures/sources.yaml"))
    db = Database(tmp_path / "status.duckdb", Path("sql"))
    db.migrate()
    record_inspection(
        registry.get("unresolved_source"),
        db,
        operation="selectedOperation",
        required_parameters={"baseYm": "202608"},
        response_row_path="response.body.items.item",
        portal_detail_url="https://data.go.kr/detail/example",
    )
    client = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"id": "one"}], "totalCount": 1}
                )
            )
        ),
        sleeper=lambda _: None,
    )

    status = probe_source(registry.get("unresolved_source"), client, db)

    assert status.status == "SCHEMA_CHANGED"


def test_inspection_command_records_reviewed_portal_metadata(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "inspection.duckdb"

    exit_code = inspection_command(
        [
            "--sources",
            "tests/fixtures/sources.yaml",
            "--source-id",
            "unresolved_source",
            "--db-path",
            str(db_path),
            "--migrations-dir",
            "sql",
            "--operation",
            "selectedOperation",
            "--required-parameter",
            "baseYm=202608",
            "--response-row-path",
            "response.body.items.item",
            "--portal-detail-url",
            "https://data.go.kr/detail/example",
        ]
    )

    assert exit_code == 0
    assert '"source_id": "unresolved_source"' in capsys.readouterr().out
    db = Database(db_path, Path("sql"))
    assert "selectedOperation" in db.query("select detail_json from source_status")[0][0]

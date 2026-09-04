from __future__ import annotations

import json
import logging
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.unit.test_tourism_ai_metrics import RUN_ID, _write_dashboard
from tests.unit.test_tourism_ai_service import _model_document
from westbusan.river_regulation.geometry import NakdongParcelGeometryCatalogue
from westbusan.river_regulation.heritage import (
    HeritageCriteriaCatalogue,
    parse_criteria_html,
)
from westbusan.river_regulation.parcel import NakdongParcelCatalogue
from westbusan.tourism_ai.api import create_app
from westbusan.tourism_ai.config import TourismAISettings
from westbusan.tourism_ai.legal_mcp import MCPResearchResult
from westbusan.tourism_ai.models import EvidenceMetric, ModelInsight
from westbusan.tourism_ai.river_policy import ModelRiverPolicyInsight
from westbusan.vacant_house.address_analysis import AddressAnalysisCatalogue


class _CountingGenerator:
    def __init__(self, document: dict[str, Any] | Exception):
        self.document = document
        self.calls = 0
        self._lock = Lock()

    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
        focus_selection: object | None,
    ) -> ModelInsight:
        del catalogue, focus_region, focus_selection
        with self._lock:
            self.calls += 1
        if isinstance(self.document, Exception):
            raise self.document
        return ModelInsight.model_validate(self.document)


class _RecoveringGenerator(_CountingGenerator):
    def generate(
        self,
        catalogue: dict[str, EvidenceMetric],
        *,
        focus_region: str,
        focus_selection: object | None,
    ) -> ModelInsight:
        if self.calls == 0:
            with self._lock:
                self.calls += 1
            raise RuntimeError("temporary_upstream_failure")
        return super().generate(
            catalogue,
            focus_region=focus_region,
            focus_selection=focus_selection,
        )


class _CountingLawClient:
    package_version = "4.12.0"

    def __init__(self) -> None:
        self.calls = 0

    def research(self, *, query: str, task: str) -> MCPResearchResult:
        assert "낙동강" in query
        assert "산책·탐방" in query
        assert "숙박시설" in query
        assert "공식 원문 URL" in query
        assert task == "action_basis"
        self.calls += 1
        return MCPResearchResult(
            tool_name="legal_research",
            arguments={"query": query, "task": task},
            package_version=self.package_version,
            text=(
                "하천법 제33조와 하천점용허가 절차를 원문으로 재확인해야 합니다. "
                "https://www.law.go.kr/법령/하천법"
            ),
            response_sha256="e" * 64,
            source_urls=("https://www.law.go.kr/법령/하천법",),
            retrieved_at=datetime.now(UTC),
        )


class _CountingRiverGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate_river_policy_insight(
        self,
        *,
        spatial_evidence: dict[str, object],
        legal_evidence: str,
    ) -> ModelRiverPolicyInsight:
        assert spatial_evidence["grade"] == "principally_restricted"
        assert "하천법 제33조" in legal_evidence
        self.calls += 1
        return ModelRiverPolicyInsight(
            headline="숙박시설은 대체입지와 최소점용 대안을 우선 검토",
            policy_insight=(
                "현재 공간판정의 원칙적 제약 등급을 유지하면서 관리청 협의 전에 "
                "사업규모와 구조물 영구성을 축소해야 합니다."
            ),
            policy_options=[
                "하천구역 밖 배후부지로 숙박기능을 이전하고 친수공원에는 비건축형 콘텐츠를 배치",
                "최소점용·가설형 대안을 작성해 홍수소통과 철거계획을 사전협의",
            ],
            required_consultations=["하천관리청 사전협의", "최신 고시·허용기준 원문확인"],
            limitations="지도와 법령조회는 허가처분을 대체하지 않습니다.",
        )


class _FailingRiverGenerator(_CountingRiverGenerator):
    def generate_river_policy_insight(
        self,
        *,
        spatial_evidence: dict[str, object],
        legal_evidence: str,
    ) -> ModelRiverPolicyInsight:
        del spatial_evidence, legal_evidence
        self.calls += 1
        raise RuntimeError("ai_unavailable")


class _UncitedLawClient(_CountingLawClient):
    def research(self, *, query: str, task: str) -> MCPResearchResult:
        result = super().research(query=query, task=task)
        return MCPResearchResult(
            tool_name=result.tool_name,
            arguments=result.arguments,
            package_version=result.package_version,
            text="공식 원문 URL이 포함되지 않은 검색 응답",
            response_sha256="f" * 64,
            source_urls=(),
            retrieved_at=result.retrieved_at,
        )


class _NamedButUnlinkedLawClient(_CountingLawClient):
    def research(self, *, query: str, task: str) -> MCPResearchResult:
        result = super().research(query=query, task=task)
        return MCPResearchResult(
            tool_name=result.tool_name,
            arguments=result.arguments,
            package_version=result.package_version,
            text=(
                "하천법 제33조, 건축법 제11조 및 관광진흥법상 등록 절차를 "
                "공식 원문에서 재확인해야 합니다."
            ),
            response_sha256="a" * 64,
            source_urls=(),
            retrieved_at=result.retrieved_at,
        )


def _settings(
    tmp_path: Path,
    *,
    daily_limit: int = 10,
    cooldown_seconds: float = 0,
) -> TourismAISettings:
    return TourismAISettings(
        openai_api_key=SecretStr("sentinel-openai-key"),
        vworld_api_key=SecretStr("sentinel-vworld-key"),
        tourism_ai_data_path=_write_dashboard(tmp_path),
        tourism_ai_cache_dir=tmp_path / "cache",
        tourism_ai_model="gpt-5.4-mini",
        tourism_ai_daily_limit=daily_limit,
        tourism_ai_client_cooldown_seconds=cooldown_seconds,
    )


def _request(region: str = "west", district: str | None = None) -> dict[str, str]:
    payload = {
        "region": region,
        "period": "latest",
        "published_run": str(RUN_ID),
    }
    if district is not None:
        payload["district"] = district
    return payload


def test_map_selection_accepts_published_transport_and_tourism_evidence(
    tmp_path: Path,
) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(create_app(_settings(tmp_path), generator=generator))
    payload = {
        **_request(),
        "selection": {
            "grid_id": "g5174_500_721_340",
            "district": "북구",
            "dong": "구포동",
            "facility_count": 11,
            "aged_facility_count": 7,
            "age_known_count": 9,
            "room_count": 84,
            "supply_gap_score": 72.5,
            "demand_score": 88.0,
            "supply_score": 15.5,
            "recommendation_kind": "new_supply",
            "transport_inbound": 327928,
            "transport_period": "2025-06",
            "nearest_tourism_poi_name": "북구문화예술회관 공연장",
            "nearest_tourism_poi_distance_m": 115.2,
            "tourism_poi_count_1000m": 4,
        },
    }

    response = client.post("/insights", json=payload)

    assert response.status_code == 200
    assert generator.calls == 1


def test_same_publication_is_generated_once(tmp_path: Path) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(create_app(_settings(tmp_path), generator=generator))

    first = client.post("/insights", json=_request())
    second = client.post("/insights", json=_request())

    assert first.status_code == second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert generator.calls == 1


def test_same_district_is_generated_once_and_other_district_has_new_cache_key(
    tmp_path: Path,
) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(create_app(_settings(tmp_path), generator=generator))

    gangseo_first = client.post("/insights", json=_request(district="gangseo"))
    gangseo_second = client.post("/insights", json=_request(district="gangseo"))
    saha = client.post("/insights", json=_request(district="saha"))

    assert gangseo_first.status_code == gangseo_second.status_code == saha.status_code == 200
    assert gangseo_first.json()["cached"] is False
    assert gangseo_second.json()["cached"] is True
    assert saha.json()["cached"] is False
    assert generator.calls == 2


def test_rule_fallback_is_not_persisted_over_a_recovered_ai_result(
    tmp_path: Path,
) -> None:
    generator = _RecoveringGenerator(_model_document())
    client = TestClient(create_app(_settings(tmp_path), generator=generator))

    fallback = client.post("/insights", json=_request(district="gangseo"))
    recovered = client.post("/insights", json=_request(district="gangseo"))
    cached = client.post("/insights", json=_request(district="gangseo"))

    assert fallback.json()["source"] == "rule_fallback"
    assert recovered.json()["source"] == "openai"
    assert cached.json()["source"] == "openai"
    assert cached.json()["cached"] is True
    assert generator.calls == 2


def test_concurrent_same_key_is_single_flight(tmp_path: Path) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(create_app(_settings(tmp_path), generator=generator))

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post("/insights", json=_request()), range(4)))

    assert all(response.status_code == 200 for response in responses)
    assert generator.calls == 1
    assert sorted(response.json()["cached"] for response in responses) == [
        False,
        True,
        True,
        True,
    ]


def test_daily_limit_returns_rule_fallback_without_second_openai_call(
    tmp_path: Path,
) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(
        create_app(_settings(tmp_path, daily_limit=1), generator=generator)
    )

    first = client.post("/insights", json=_request("west"))
    second = client.post("/insights", json=_request("east"))

    assert first.json()["source"] == "openai"
    assert second.status_code == 200
    assert second.json()["source"] == "rule_fallback"
    assert generator.calls == 1


def test_per_client_cooldown_rejects_second_distinct_generation(
    tmp_path: Path,
) -> None:
    generator = _CountingGenerator(_model_document())
    client = TestClient(
        create_app(
            _settings(tmp_path, cooldown_seconds=60),
            generator=generator,
        )
    )

    assert client.post("/insights", json=_request("west")).status_code == 200
    response = client.post("/insights", json=_request("east"))

    assert response.status_code == 429
    assert "sentinel" not in response.text


def test_body_larger_than_two_kib_is_rejected_before_parsing(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
        )
    )
    body = json.dumps({**_request(), "padding": "x" * 2200})

    response = client.post(
        "/insights", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413


def test_non_json_content_type_is_rejected(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
        )
    )

    response = client.post("/insights", content="region=west")

    assert response.status_code == 415


def test_health_and_error_response_never_return_api_key(tmp_path: Path) -> None:
    secret = "sentinel-openai-key"
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(RuntimeError(secret)),
        )
    )

    health = client.get("/healthz")
    response = client.post("/insights", json=_request())

    assert health.json() == {"status": "ok", "data_ready": True}
    assert secret not in health.text
    assert secret not in response.text
    assert response.json()["source"] == "rule_fallback"


def test_readyz_returns_component_evidence_without_secrets(
    tmp_path: Path, monkeypatch: Any
) -> None:
    secret = "sentinel-openai-key"
    settings = _settings(tmp_path)
    settings.openai_api_key = SecretStr(secret)
    monkeypatch.setattr(
        "westbusan.tourism_ai.api.readiness_report",
        lambda current: {
            "status": "degraded",
            "ready": True,
            "checks": {"law_mcp": {"status": "degraded"}},
        },
    )
    client = TestClient(create_app(settings, generator=_CountingGenerator(_model_document())))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "degraded"
    assert secret not in response.text


def test_missing_openai_key_keeps_rule_fallback_available(tmp_path: Path) -> None:
    settings = TourismAISettings(
        tourism_ai_data_path=_write_dashboard(tmp_path),
        tourism_ai_cache_dir=tmp_path / "cache",
        tourism_ai_client_cooldown_seconds=0,
    )
    client = TestClient(create_app(settings))

    response = client.post("/insights", json=_request())

    assert response.status_code == 200
    assert response.json()["source"] == "rule_fallback"


def test_corrupt_cache_is_replaced_with_valid_response(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    generator = _CountingGenerator(_model_document())
    app = create_app(settings, generator=generator)
    client = TestClient(app)
    assert client.post("/insights", json=_request()).status_code == 200
    cache_files = list(settings.tourism_ai_cache_dir.glob("insight-*.json"))
    assert len(cache_files) == 1
    cache_files[0].write_text("not-json", encoding="utf-8")

    response = client.post("/insights", json=_request())

    assert response.status_code == 200
    assert response.json()["cached"] is False
    assert generator.calls == 2
    assert list(settings.tourism_ai_cache_dir.glob("*.invalid"))


def test_vworld_tile_is_server_proxied_without_disclosing_key(
    tmp_path: Path, caplog: Any
) -> None:
    """Catches a browser-visible key or a fixed static map returning as a tile."""
    secret = "sentinel-vworld-key"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.vworld.kr"
        assert request.url.path == (
            f"/req/wmts/1.0.0/{secret}/Base/14/6449/13969.png"
        )
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\nreviewed-map",
            headers={"content-type": "image/png"},
        )

    upstream = httpx.Client(transport=httpx.MockTransport(respond))
    caplog.set_level(logging.INFO)
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
            vworld_client=upstream,
        )
    )

    response = client.get("/vworld/tiles/14/13969/6449.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG")
    assert secret not in response.text
    assert secret not in caplog.text


def test_vworld_tile_retries_transient_upstream_failure(
    tmp_path: Path,
) -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502)
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\nrecovered-map",
            headers={"content-type": "image/png"},
        )

    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
            vworld_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )
    )

    response = client.get("/vworld/tiles/14/13969/6449.png")

    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\nrecovered-map"
    assert attempts == 2


def test_vworld_tile_returns_transparent_png_after_repeated_timeouts(
    tmp_path: Path,
) -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("temporary timeout", request=request)

    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
            vworld_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )
    )

    response = client.get("/vworld/tiles/14/13969/6449.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.content[16:24] == b"\x00\x00\x00\x01\x00\x00\x00\x01"
    assert response.content[25] == 6
    chunk_length = int.from_bytes(response.content[33:37], byteorder="big")
    assert response.content[37:41] == b"IDAT"
    scanline = zlib.decompress(response.content[41 : 41 + chunk_length])
    assert scanline == b"\x00\x00\x00\x00\x00"
    assert attempts == 2


def test_vworld_tile_returns_transparent_png_after_repeated_server_errors(
    tmp_path: Path,
) -> None:
    statuses = iter((500, 502))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses))

    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
            vworld_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )
    )

    response = client.get("/vworld/tiles/14/13969/6449.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert response.content[16:24] == b"\x00\x00\x00\x01\x00\x00\x00\x01"


def test_vworld_tile_rejects_invalid_coordinates_before_upstream(
    tmp_path: Path,
) -> None:
    """Catches arbitrary URL expansion through the credentialed tile proxy."""
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
        )
    )

    assert client.get("/vworld/tiles/20/1/1.png").status_code == 422
    assert client.get("/vworld/tiles/14/20000/1.png").status_code == 422
    assert client.get("/vworld/tiles/14/1/20000.png").status_code == 422


def test_vworld_tile_is_unavailable_without_server_key(tmp_path: Path) -> None:
    """Catches an accidental client-side fallback that would expose credentials."""
    settings = _settings(tmp_path).model_copy(update={"vworld_api_key": None})
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
        )
    )

    response = client.get("/vworld/tiles/14/13969/6449.png")

    assert response.status_code == 503
    assert response.json() == {"detail": "vworld_unavailable"}


def test_regulation_point_is_server_queried_and_secret_safe(tmp_path: Path) -> None:
    secret = "sentinel-vworld-key"
    requested_datasets: set[str] = set()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/req/data"
        assert request.url.params["key"] == secret
        assert request.url.params["geomFilter"] == "POINT(128.953 35.117)"
        dataset = request.url.params["data"]
        requested_datasets.add(dataset)
        if dataset != "LT_C_UM901":
            return httpx.Response(200, json={"response": {"status": "NOT_FOUND"}})
        return httpx.Response(
            200,
            json={
                "response": {
                    "status": "OK",
                    "result": {
                        "featureCollection": {
                            "features": [
                                {
                                    "properties": {
                                        "dgm_nm": "낙동강하구 습지보호지역",
                                        "private": "never-publish",
                                    },
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [[
                                            [128.95, 35.11],
                                            [128.96, 35.11],
                                            [128.96, 35.12],
                                            [128.95, 35.12],
                                            [128.95, 35.11],
                                        ]],
                                    },
                                }
                            ]
                        }
                    },
                }
            },
        )

    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
            vworld_client=httpx.Client(transport=httpx.MockTransport(respond)),
        )
    )

    response = client.get(
        "/regulations/point",
        params={
            "longitude": 128.953,
            "latitude": 35.117,
            "activity": "lodging",
            "river_zone": "general_conservation",
        },
    )

    assert response.status_code == 200
    assert len(requested_datasets) == 8
    assert response.json()["grade"] == "principally_restricted"
    assert response.json()["complete"] is True
    assert response.json()["matches"][0]["category"] == "wetland"
    assert secret not in response.text
    assert "private" not in response.text


def test_regulation_point_without_key_is_explicitly_partial(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"vworld_api_key": None})
    client = TestClient(
        create_app(settings, generator=_CountingGenerator(_model_document()))
    )

    response = client.get(
        "/regulations/point",
        params={
            "longitude": 128.953,
            "latitude": 35.117,
            "activity": "walking",
            "river_zone": "waterfront",
        },
    )

    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert response.json()["missing_categories"] == [
        "wetland",
        "heritage",
        "urban_park",
        "land_use",
    ]
    assert {item["status"] for item in response.json()["layer_statuses"]} == {
        "provider_error"
    }
    assert response.json()["parcel_planning"]["status"] == "pnu_required"


def test_regulation_point_uses_independent_nakdong_parcel_catalogue(
    tmp_path: Path,
) -> None:
    pnu = "2632010100100010000"
    catalogue = NakdongParcelCatalogue.from_records(
        snapshot_id="nakdong-test-1",
        checked_at="2026-08-28T00:00:00+00:00",
        parcels=[
            {
                "pnu": pnu,
                "land_use_status": "matched",
                "land_use_designations": [
                    "제2종일반주거지역",
                    "개발행위허가제한지역",
                ],
                "land_use_response_sha256": "a" * 64,
                "land_characteristics_status": "not_found",
                "land_characteristics_response_sha256": "b" * 64,
                "land_characteristics": None,
                "source_date": "2026-08-27",
            }
        ],
    )
    settings = _settings(tmp_path).model_copy(update={"vworld_api_key": None})
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
            parcel_catalogue=catalogue,
        )
    )

    response = client.get(
        "/regulations/point",
        params={
            "longitude": 129.01025,
            "latitude": 35.2061,
            "activity": "lodging",
            "river_zone": "waterfront",
            "pnu": pnu,
        },
    )

    assert response.status_code == 200
    result = response.json()["parcel_planning"]
    assert result["status"] == "matched"
    assert result["grade"] == "principally_restricted"
    assert result["snapshot_id"] == "nakdong-test-1"
    assert response.json()["complete"] is False


def test_regulation_point_resolves_pnu_from_published_parcel_geometry(
    tmp_path: Path,
) -> None:
    from shapely.geometry import Polygon

    pnu = "2632010100100010000"
    parcel_catalogue = NakdongParcelCatalogue.from_records(
        snapshot_id="nakdong-test-auto-pnu",
        checked_at="2026-08-29T00:00:00+00:00",
        parcels=[
            {
                "pnu": pnu,
                "land_use_status": "matched",
                "land_use_designations": ["화명지구단위계획구역"],
                "land_use_response_sha256": "a" * 64,
                "land_characteristics_status": "not_found",
                "land_characteristics_response_sha256": "b" * 64,
                "land_characteristics": None,
                "source_date": "2026-08-28",
            }
        ],
    )
    geometry_catalogue = NakdongParcelGeometryCatalogue.from_records(
        snapshot_id="geometry-test-auto-pnu",
        checked_at="2026-08-29T00:00:00+00:00",
        records=[
            {
                "pnu": pnu,
                "status": "matched",
                "request_identity": "request:test",
                "response_sha256": "c" * 64,
                "geometry": Polygon(
                    [
                        (129.0100, 35.2060),
                        (129.0105, 35.2060),
                        (129.0105, 35.2065),
                        (129.0100, 35.2065),
                    ]
                ),
                "geometry_hash": "d" * 64,
                "source_date": "2026-08-28",
            }
        ],
    )
    settings = _settings(tmp_path).model_copy(update={"vworld_api_key": None})
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
            parcel_catalogue=parcel_catalogue,
            parcel_geometry_catalogue=geometry_catalogue,
        )
    )

    response = client.get(
        "/regulations/point",
        params={
            "longitude": 129.01025,
            "latitude": 35.2061,
            "activity": "lodging",
            "river_zone": "waterfront",
        },
    )

    assert response.status_code == 200
    assert response.json()["parcel_resolution"] == {
        "status": "matched",
        "pnu": pnu,
        "candidate_pnus": [pnu],
        "snapshot_id": "geometry-test-auto-pnu",
        "checked_at": "2026-08-29T00:00:00+00:00",
        "target_count": 1,
        "matched_count": 1,
        "complete": True,
    }
    assert response.json()["parcel_planning"]["status"] == "matched"
    assert len(response.json()["action_screenings"]) == 9
    selected = next(
        item for item in response.json()["action_screenings"] if item["selected"]
    )
    assert selected["activity"] == "lodging"
    assert selected["grade"] == "principally_restricted"
    assert response.json()["combined_grade"] == selected["grade"]
    assert response.json()["combined_label"] == selected["status_label"]


def test_river_policy_insight_uses_cached_legal_evidence_and_ai_result(
    tmp_path: Path,
) -> None:
    law_client = _CountingLawClient()
    river_generator = _CountingRiverGenerator()
    settings = _settings(tmp_path).model_copy(
        update={
            "vworld_api_key": None,
            "tourism_ai_legal_db_path": tmp_path / "law-evidence.duckdb",
        }
    )
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
            law_mcp_client=law_client,
            river_policy_generator=river_generator,
        )
    )
    payload = {
        "longitude": 128.953,
        "latitude": 35.117,
        "activity": "lodging",
        "river_zone": "waterfront",
        "roof_type": "unknown",
    }

    first = client.post("/regulations/insight", json=payload)
    second = client.post("/regulations/insight", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["deterministic_grade"] == "principally_restricted"
    assert first.json()["legal_evidence_status"] == "retrieved"
    assert first.json()["legal_evidence_source"] == "curated_registry_and_mcp"
    assert first.json()["legal_source_urls"] == [
        "https://www.law.go.kr/법령/하천법",
        "https://www.law.go.kr/법령/건축법",
        "https://www.law.go.kr/법령/관광진흥법",
    ]
    assert first.json()["source"] == "openai"
    assert first.json()["prompt_version"].endswith("-river-v6")
    assert len(first.json()["action_screenings"]) == 9
    selected = next(
        item for item in first.json()["action_screenings"] if item["selected"]
    )
    assert selected["activity"] == "lodging"
    assert selected["grade"] == first.json()["deterministic_grade"]
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert law_client.calls == 1
    assert river_generator.calls == 1


def test_river_policy_uses_curated_legal_basis_when_mcp_response_is_uncited(
    tmp_path: Path,
) -> None:
    law_client = _UncitedLawClient()
    river_generator = _CountingRiverGenerator()
    settings = _settings(tmp_path).model_copy(
        update={
            "vworld_api_key": None,
            "tourism_ai_legal_db_path": tmp_path / "law-evidence.duckdb",
        }
    )
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
            law_mcp_client=law_client,
            river_policy_generator=river_generator,
        )
    )
    payload = {
        "longitude": 128.953,
        "latitude": 35.117,
        "activity": "lodging",
        "river_zone": "waterfront",
        "roof_type": "unknown",
    }

    first = client.post("/regulations/insight", json=payload)
    second = client.post("/regulations/insight", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["legal_evidence_status"] == "retrieved"
    assert first.json()["legal_evidence_source"] == "curated_registry"
    assert first.json()["legal_source_urls"] == [
        "https://www.law.go.kr/법령/하천법",
        "https://www.law.go.kr/법령/건축법",
        "https://www.law.go.kr/법령/관광진흥법",
    ]
    assert {basis["code"] for basis in first.json()["legal_bases"]} == {
        "river_management_zone",
        "river_occupation",
        "building_permission",
        "tourism_business_registration",
    }
    assert first.json()["source"] == "openai"
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert "인허가 처분 또는 관리청 공식의견을 대체하지 않습니다" in (
        first.json()["limitations"]
    )
    assert law_client.calls == 1
    assert river_generator.calls == 1


def test_river_policy_attaches_verified_official_urls_to_named_mcp_laws(
    tmp_path: Path,
) -> None:
    law_client = _NamedButUnlinkedLawClient()
    settings = _settings(tmp_path).model_copy(
        update={
            "vworld_api_key": None,
            "tourism_ai_legal_db_path": tmp_path / "law-evidence.duckdb",
        }
    )
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
            law_mcp_client=law_client,
            river_policy_generator=_CountingRiverGenerator(),
        )
    )
    payload = {
        "longitude": 128.953,
        "latitude": 35.117,
        "activity": "lodging",
        "river_zone": "waterfront",
        "roof_type": "unknown",
    }

    first = client.post("/regulations/insight", json=payload)
    second = client.post("/regulations/insight", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["legal_evidence_source"] == "curated_registry_and_mcp"
    assert first.json()["legal_mcp_package_version"] == "4.12.0"
    assert first.json()["legal_source_urls"] == [
        "https://www.law.go.kr/법령/하천법",
        "https://www.law.go.kr/법령/건축법",
        "https://www.law.go.kr/법령/관광진흥법",
    ]
    assert second.json()["cached"] is True
    assert law_client.calls == 1


def test_river_policy_keeps_grounded_basic_explanation_when_ai_is_unavailable(
    tmp_path: Path,
) -> None:
    river_generator = _FailingRiverGenerator()
    settings = _settings(tmp_path).model_copy(
        update={
            "vworld_api_key": None,
            "tourism_ai_legal_db_path": tmp_path / "law-evidence.duckdb",
        }
    )
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
            law_mcp_client=_UncitedLawClient(),
            river_policy_generator=river_generator,
        )
    )

    response = client.post(
        "/regulations/insight",
        json={
            "longitude": 128.953,
            "latitude": 35.117,
            "activity": "lodging",
            "river_zone": "waterfront",
            "roof_type": "unknown",
        },
    )

    assert response.status_code == 200
    assert response.json()["source"] == "rule_fallback"
    assert response.json()["legal_evidence_status"] == "retrieved"
    assert response.json()["legal_evidence_source"] == "curated_registry"
    assert "하천법" in response.json()["policy_insight"]
    assert "숙박시설" in response.json()["policy_insight"]
    assert "현재 계획대로" in response.json()["policy_insight"]
    assert "자료를 더 확인" in response.json()["policy_insight"]
    assert len(response.json()["action_screenings"]) == 9
    by_activity = {
        item["activity"]: item for item in response.json()["action_screenings"]
    }
    assert by_activity["lodging"]["grade"] == "principally_restricted"
    assert by_activity["walking"]["grade"] == "conditional"
    assert any("대체입지" in item for item in response.json()["policy_options"])
    assert not any("가설" in item for item in response.json()["policy_options"])
    assert any("숙박 원안" in item for item in response.json()["policy_options"])
    assert any("사업계획" in item for item in response.json()["required_consultations"])
    assert "인허가 처분 또는 관리청 공식의견을 대체하지 않습니다" in (
        response.json()["limitations"]
    )
    assert river_generator.calls == 1


def test_regulation_point_rejects_invalid_pnu(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"vworld_api_key": None})
    client = TestClient(
        create_app(settings, generator=_CountingGenerator(_model_document()))
    )

    response = client.get(
        "/regulations/point",
        params={
            "longitude": 129.01025,
            "latitude": 35.2061,
            "activity": "lodging",
            "river_zone": "waterfront",
            "pnu": "123",
        },
    )

    assert response.status_code == 422


def test_regulation_point_uses_cached_heritage_criteria_without_upstream_call(
    tmp_path: Path,
) -> None:
    from shapely.geometry import box, mapping

    criteria = parse_criteria_html(
        """<table><tbody>
        <tr><td>2구역</td><td></td><td>건축물 최고높이 11m 이하</td>
            <td>건축물 최고높이 15m 이하</td><td>2</td></tr>
        <tr><td>공통</td><td></td><td><input id="hidden_pmpgSeid"
            value="PMPG00000812">최고높이는 옥탑 포함</td></tr>
        </tbody></table>"""
    )
    catalogue = HeritageCriteriaCatalogue.from_records(
        snapshot_id="cached-heritage-1",
        source_checked_at="2026-08-28T00:00:00+00:00",
        designations=[],
        criteria_zones=[
            {
                "layer_name": "CHL_PMPG_AS_1",
                "gid": 1,
                "pmpg_seid": criteria.pmpg_seid,
                "zone_name": "2구역",
                "geometry": mapping(box(128.95, 35.11, 128.96, 35.12)),
                "criteria": criteria.as_dict(),
            }
        ],
    )
    settings = _settings(tmp_path).model_copy(update={"vworld_api_key": None})
    client = TestClient(
        create_app(
            settings,
            generator=_CountingGenerator(_model_document()),
            heritage_catalogue=catalogue,
        )
    )

    response = client.get(
        "/regulations/point",
        params={
            "longitude": 128.955,
            "latitude": 35.115,
            "activity": "lodging",
            "river_zone": "waterfront",
            "height_m": 10,
            "roof_type": "flat",
        },
    )

    assert response.status_code == 200
    result = response.json()["heritage_criteria"]
    assert result["code"] == "within_published_criteria"
    assert result["limit_m"] == 11
    assert result["snapshot_id"] == "cached-heritage-1"
    assert result["legal_effect"] is False


def test_vworld_parcel_geocode_is_server_proxied_and_redacted(tmp_path: Path) -> None:
    """Catches browser-side credentials or exact-address AI without a reviewed point."""
    secret = "sentinel-vworld-key"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.vworld.kr"
        assert request.url.path == "/req/search"
        assert request.url.params["key"] == secret
        assert request.url.params["category"] == "parcel"
        return httpx.Response(
            200,
            content=(Path("tests/fixtures/spatial") / "vworld_address_success.json").read_bytes(),
        )

    upstream = httpx.Client(transport=httpx.MockTransport(respond))
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
            vworld_client=upstream,
        )
    )

    response = client.post(
        "/vworld/geocode",
        json={"address": "부산광역시 북구 구포동 1-1"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "matched",
        "longitude": 129.01025,
        "latitude": 35.2061,
        "district": "북구",
        "crs": "EPSG:4326",
        "pnu": None,
    }
    assert secret not in response.text
    assert "response_hash" not in response.text


def test_vworld_parcel_geocode_rejects_non_busan_or_missing_key(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
        )
    )
    assert client.post(
        "/vworld/geocode", json={"address": "서울특별시 중구 1-1"}
    ).status_code == 422

    settings = _settings(tmp_path).model_copy(update={"vworld_api_key": None})
    without_key = TestClient(
        create_app(settings, generator=_CountingGenerator(_model_document()))
    )
    response = without_key.post(
        "/vworld/geocode", json={"address": "부산광역시 북구 구포동 1-1"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "vworld_unavailable"}


def test_vacant_address_analysis_is_post_only_and_evidence_bound(
    tmp_path: Path,
) -> None:
    """Catches browser coordinates or a raw address-only AI answer bypassing PNU evidence."""
    from datetime import date
    from uuid import uuid4

    from shapely.geometry import box, mapping

    pnu = "2632010100100010000"
    inventory_run_id, hub_run_id = uuid4(), uuid4()
    geometry = box(129.00, 35.20, 129.02, 35.22)
    catalogue = AddressAnalysisCatalogue(
        inventory_run_id=inventory_run_id,
        hub_run_id=hub_run_id,
        source_date=date(2026, 8, 21),
        vacant_geometries={pnu: geometry},
        hub_members={pnu: "vh-reviewed"},
        hub_geometries={"vh-reviewed": geometry},
        hub_ranks={"vh-reviewed": 1},
        hub_parcel_counts={"vh-reviewed": 3},
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/req/search":
            document = json.loads(
                (Path("tests/fixtures/spatial") / "vworld_address_success.json")
                .read_text(encoding="utf-8")
            )
            document["response"]["result"]["items"][0]["id"] = pnu
            return httpx.Response(200, json=document)
        assert request.url.path == "/req/data"
        return httpx.Response(
            200,
            json={
                "response": {
                    "status": "OK",
                    "result": {
                        "featureCollection": {
                            "features": [
                                {
                                    "properties": {"pnu": pnu, "sourceDate": "2026-08-21"},
                                    "geometry": mapping(geometry),
                                }
                            ]
                        }
                    },
                }
            },
        )

    client = TestClient(
        create_app(
            _settings(tmp_path),
            generator=_CountingGenerator(_model_document()),
            vworld_client=httpx.Client(transport=httpx.MockTransport(respond)),
            vacant_catalogue=catalogue,
        )
    )
    response = client.post(
        "/vacant/address-analysis",
        json={"address": "부산광역시 북구 시험동 1-1"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "address": "부산광역시 북구 시험동 1-1",
        "status": "in_contiguous_hub",
        "hub_id": "vh-reviewed",
        "hub_rank": 1,
        "hub_parcel_count": 3,
        "inventory_run_id": str(inventory_run_id),
        "hub_run_id": str(hub_run_id),
        "source_date": "2026-08-21",
        "interpretation": "연속 필지군 내부의 게시 빈집 필지입니다.",
        "limitation": "관광숙박 전환 가능 여부는 토지이용·건축·소방·위생 등 별도 행정검토가 필요합니다.",
    }
    assert client.get("/vacant/address-analysis").status_code == 405
    assert client.post(
        "/vacant/address-analysis",
        json={
            "address": "부산광역시 북구 시험동 1-1",
            "longitude": 129.01,
        },
    ).status_code == 422

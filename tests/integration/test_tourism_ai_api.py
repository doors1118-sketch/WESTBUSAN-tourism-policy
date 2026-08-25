from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.unit.test_tourism_ai_metrics import RUN_ID, _write_dashboard
from tests.unit.test_tourism_ai_service import _model_document
from westbusan.tourism_ai.api import create_app
from westbusan.tourism_ai.config import TourismAISettings
from westbusan.tourism_ai.models import EvidenceMetric, ModelInsight
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

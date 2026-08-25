from __future__ import annotations

import json
import runpy
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import box

import westbusan.accessibility.build as accessibility_build
from westbusan.accessibility.poi import parse_kto_poi_rows, review_poi

FIXTURE = Path("tests/fixtures/accessibility/kto_area_based_success.json")
BUSAN_BOUNDARY = box(128.7, 34.8, 129.35, 35.4)


def test_parse_kto_poi_preserves_official_identity_and_wgs84() -> None:
    poi = parse_kto_poi_rows(FIXTURE.read_bytes())[0]

    assert poi.content_id == "126848"
    assert poi.title == "구포시장"
    assert poi.content_type_id == "38"
    assert poi.category_codes == ("A04", "A0401", "A04010200")
    assert poi.longitude == pytest.approx(129.0028)
    assert poi.latitude == pytest.approx(35.2054)
    assert poi.source_url.endswith("/KorService2/areaBasedList2")


def test_parse_kto_poi_rejects_missing_identity_or_coordinate() -> None:
    body = b'{"response":{"body":{"items":{"item":[{"title":"no id"}]}}}}'

    with pytest.raises(ValueError, match="contentid"):
        parse_kto_poi_rows(body)


def test_review_poi_rejects_point_outside_busan() -> None:
    poi = parse_kto_poi_rows(FIXTURE.read_bytes())[0]
    outside = replace(poi, longitude=127.0, latitude=37.5)

    assert review_poi(outside, BUSAN_BOUNDARY, None).status == "outside_busan"


def test_review_poi_rejects_expected_district_mismatch() -> None:
    poi = parse_kto_poi_rows(FIXTURE.read_bytes())[0]

    review = review_poi(poi, BUSAN_BOUNDARY, "사하구")

    assert review.status == "district_mismatch"
    assert review.accepted is False


def test_review_poi_accepts_busan_point_with_matching_district() -> None:
    poi = parse_kto_poi_rows(FIXTURE.read_bytes())[0]

    review = review_poi(poi, BUSAN_BOUNDARY, "북구")

    assert review.status == "accepted"
    assert review.accepted is True


def test_kto_publisher_never_uses_httpx_url_bearing_status_error() -> None:
    source = Path("scripts/publish_accessibility_snapshot.py").read_text(
        encoding="utf-8"
    )

    assert "raise_for_status" not in source
    assert "kto_http_status:{response.status_code}" in source


def test_kto_publisher_collects_every_busan_sigungu(monkeypatch) -> None:
    """Catches the provider's unscoped Busan query omitting sigungu code 1."""
    districts = {
        "1": "강서구",
        "2": "금정구",
        "3": "기장군",
        "4": "남구",
        "5": "동구",
        "6": "동래구",
        "7": "부산진구",
        "8": "북구",
        "9": "사상구",
        "10": "사하구",
        "11": "서구",
        "12": "수영구",
        "13": "연제구",
        "14": "영도구",
        "15": "중구",
        "16": "해운대구",
    }

    class Response:
        status_code = 200

        def __init__(self, rows: list[dict[str, str]]) -> None:
            payload = {
                "response": {
                    "header": {"resultCode": "0000", "resultMsg": "OK"},
                    "body": {
                        "items": {"item": rows},
                        "totalCount": len(rows),
                        "pageNo": 1,
                        "numOfRows": 1000,
                    },
                }
            }
            self.content = json.dumps(payload).encode()

        def json(self):
            return json.loads(self.content)

    class Client:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, _url, *, params):
            code = params.get("sigunguCode")
            selected = districts if code is None else {code: districts[code]}
            rows = [
                {
                    "contentid": code,
                    "title": f"{district} 관광지",
                    "contenttypeid": "12",
                    "addr1": f"부산광역시 {district}",
                    "mapx": "128.9000",
                    "mapy": "35.1700",
                    "modifiedtime": "20260825000000",
                }
                for code, district in selected.items()
                if code != "1" or params.get("sigunguCode") == "1"
            ]
            return Response(rows)

    publisher = runpy.run_path("scripts/publish_accessibility_snapshot.py")
    monkeypatch.setattr(publisher["httpx"], "Client", Client)

    rows, _response_hash = publisher["_fetch_all"]("secret", timeout=1.0)

    assert len(rows) == 16
    assert {row.address.split()[1] for row in rows} == set(districts.values())


def test_gangseo_is_not_misclassified_as_shorter_seogu() -> None:
    publisher = runpy.run_path("scripts/publish_accessibility_snapshot.py")

    assert publisher["_district_name_from_address"](
        "부산광역시 강서구 명지동"
    ) == "강서구"
    assert accessibility_build._district_from_address(
        "부산광역시 강서구 명지동"
    ) == "강서구"


def test_tourism_revision_is_bound_to_district_classifier_version(
    monkeypatch,
) -> None:
    poi = parse_kto_poi_rows(FIXTURE.read_bytes())[0]
    initial = accessibility_build._tourism_poi_revision((poi,))

    monkeypatch.setattr(
        accessibility_build,
        "_TOURISM_DISTRICT_CLASSIFIER_VERSION",
        "future-classifier",
    )

    assert accessibility_build._tourism_poi_revision((poi,)) != initial

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import box

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

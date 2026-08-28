from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from shapely.geometry import box, mapping

from westbusan.db import Database
from westbusan.river_regulation.heritage import (
    HeritageCriteriaCatalogue,
    HeritageProject,
    collect_heritage_snapshot,
    load_heritage_catalogue,
    parse_criteria_html,
    publish_heritage_snapshot,
)

CRITERIA_HTML = """
<table>
  <thead><tr><th>구분</th><th>범례</th><th>평지붕</th><th>경사지붕</th></tr></thead>
  <tbody>
    <tr><td>1구역</td><td></td><td colspan="2">ㅇ 개별 심의</td><td>1</td></tr>
    <tr><td>2구역</td><td></td><td>ㅇ 건축물 최고높이 11m 이하</td>
        <td>ㅇ 건축물 최고높이 15m 이하</td><td>2</td></tr>
    <tr><td>5구역</td><td></td>
        <td colspan="2">ㅇ 지방자치단체 도시계획조례 등 관련법률에 따라 처리</td>
        <td>5</td></tr>
    <tr><td>공통</td><td></td><td colspan="3">
        <input type="hidden" id="hidden_pmpgSeid" value="PMPG00000812">
        ㅇ 건축물의 최고높이는 옥탑 등을 포함함<br>ㅇ 경관조명은 개별 심의함
    </td></tr>
  </tbody>
</table>
"""


def test_parser_preserves_official_text_and_extracts_only_structured_height() -> None:
    parsed = parse_criteria_html(CRITERIA_HTML)

    assert parsed.pmpg_seid == "PMPG00000812"
    assert parsed.zones["1구역"].decision == "individual_review"
    assert parsed.zones["2구역"].flat_roof_max_height_m == 11
    assert parsed.zones["2구역"].sloped_roof_max_height_m == 15
    assert parsed.zones["5구역"].decision == "other_law_review"
    assert "경관조명은 개별 심의" in parsed.common_text


def test_catalogue_uses_direct_designation_before_permission_criteria() -> None:
    catalogue = HeritageCriteriaCatalogue.from_records(
        snapshot_id="snapshot-1",
        source_checked_at="2026-08-28T00:00:00+00:00",
        designations=[
            {
                "layer_name": "CHL_SPCN_AS",
                "gid": 10,
                "heritage_name": "시험 국가유산",
                "geometry": mapping(box(128.99, 35.19, 129.01, 35.21)),
            }
        ],
        criteria_zones=[],
    )

    decision = catalogue.review_point(
        longitude=129.0,
        latitude=35.2,
        project=HeritageProject(activity="culture", height_m=8, roof_type="flat"),
    )

    assert decision.code == "direct_designation_overlap"
    assert decision.legal_effect is False
    assert "현상변경 허가" in decision.next_check


def test_catalogue_applies_zone_height_and_never_calls_it_a_final_permission() -> None:
    parsed = parse_criteria_html(CRITERIA_HTML)
    catalogue = HeritageCriteriaCatalogue.from_records(
        snapshot_id="snapshot-2",
        source_checked_at="2026-08-28T00:00:00+00:00",
        designations=[],
        criteria_zones=[
            {
                "layer_name": "CHL_PMPG_AS_1",
                "gid": 20,
                "pmpg_seid": parsed.pmpg_seid,
                "zone_name": "2구역",
                "geometry": mapping(box(128.99, 35.19, 129.01, 35.21)),
                "criteria": parsed.as_dict(),
            }
        ],
    )

    within = catalogue.review_point(
        longitude=129.0,
        latitude=35.2,
        project=HeritageProject(activity="lodging", height_m=10, roof_type="flat"),
    )
    exceeded = catalogue.review_point(
        longitude=129.0,
        latitude=35.2,
        project=HeritageProject(activity="lodging", height_m=12, roof_type="flat"),
    )
    missing_input = catalogue.review_point(
        longitude=129.0,
        latitude=35.2,
        project=HeritageProject(activity="lodging", height_m=None, roof_type="flat"),
    )

    assert within.code == "within_published_criteria"
    assert within.limit_m == 11
    assert "허가 가능" not in within.label
    assert exceeded.code == "exceeds_published_criteria"
    assert missing_input.code == "project_input_required"


def test_snapshot_is_published_atomically_and_reloaded_from_duckdb(tmp_path: Path) -> None:
    db = Database(tmp_path / "heritage.duckdb", Path("sql"))
    db.migrate()
    parsed = parse_criteria_html(CRITERIA_HTML)
    run_id = uuid4()

    publish_heritage_snapshot(
        db,
        run_id=run_id,
        checked_at=datetime(2026, 8, 28, tzinfo=UTC),
        bounds=(128.8, 35.0, 129.1, 35.3),
        designations=[
            {
                "layer_name": "CHL_SPCN_AS",
                "gid": 100,
                "cp_cd": "16000179000000021",
                "heritage_name": "낙동강 하류 철새 도래지",
                "geometry": mapping(box(128.90, 35.10, 128.91, 35.11)),
            }
        ],
        criteria_zones=[
            {
                "layer_name": "CHL_PMPG_AS_1",
                "gid": 101,
                "pmpg_seid": parsed.pmpg_seid,
                "zone_code": "0200",
                "zone_name": "2구역",
                "geometry": mapping(box(128.91, 35.11, 128.92, 35.12)),
                "criteria": parsed.as_dict(),
                "source_url": "https://gis-heritage.go.kr/",
            }
        ],
    )

    publication = db.query(
        "select run_id from heritage_criteria_publication_current where publication_key = 'current'"
    )
    assert publication == [(run_id,)]
    assert json.loads(
        db.scalar(
            "select criteria_json from heritage_criteria_zone_snapshot where run_id = ?",
            [run_id],
        )
    )["pmpg_seid"] == "PMPG00000812"

    loaded = load_heritage_catalogue(db.connection)
    assert loaded.snapshot_id == str(run_id)
    assert loaded.review_point(
        longitude=128.915,
        latitude=35.115,
        project=HeritageProject(activity="lodging", height_m=10, roof_type="flat"),
    ).code == "within_published_criteria"


def test_collector_fetches_official_layers_and_one_detail_per_criteria_group() -> None:
    detail_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_calls
        if request.url.path.endswith("/services/wfs"):
            layer = request.url.params["TYPENAME"]
            if layer == "CHL_PMPG_AS_1":
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": mapping(box(128.9, 35.1, 128.91, 35.11)),
                                "properties": {
                                    "GID": gid,
                                    "PMPG_SEID": "PMPG00000812",
                                    "ZON_CD": code,
                                    "ZON_NM": name,
                                },
                            }
                            for gid, code, name in (
                                (1, "0100", "1구역"),
                                (2, "0200", "2구역"),
                            )
                        ],
                    },
                )
            if layer == "CHL_SPCN_AS":
                return httpx.Response(
                    200,
                    json={
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": mapping(box(128.92, 35.12, 128.93, 35.13)),
                                "properties": {
                                    "GID": 9,
                                    "CP_CD": "16000179000000021",
                                    "CPH_NM": "낙동강 하류 철새 도래지",
                                },
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        if request.url.path.endswith("/NewGisChaSecGGID.do"):
            detail_calls += 1
            return httpx.Response(200, text=CRITERIA_HTML)
        raise AssertionError(str(request.url))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collected = collect_heritage_snapshot(
            client,
            bounds=(128.8, 35.0, 129.1, 35.3),
        )

    assert len(collected.designations) == 1
    assert len(collected.criteria_zones) == 2
    assert detail_calls == 1
    assert collected.criteria_zones[1]["criteria"]["zones"]["2구역"][
        "flat_roof_max_height_m"
    ] == 11


def test_collector_fails_closed_on_non_json_wfs_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="<html>error</html>")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="heritage_wfs_invalid_response"),
    ):
        collect_heritage_snapshot(client, bounds=(128.8, 35.0, 129.1, 35.3))

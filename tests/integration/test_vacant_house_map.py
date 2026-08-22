from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import UUID

from shapely import to_wkb
from shapely.geometry import box

from westbusan.vacant_house.map_export import (
    export_vacant_house_map_current,
    validate_vacant_house_map_bundle,
)

HUB_RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
INVENTORY_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _PublishedVacantMapConnection:
    def __init__(self) -> None:
        parcels = [
            box(128.9900, 35.2000, 128.9902, 35.2002),
            box(128.9902, 35.2000, 128.9904, 35.2002),
            box(128.9904, 35.2000, 128.9906, 35.2002),
        ]
        self.parcels = parcels
        self.hub = parcels[0].union(parcels[1]).union(parcels[2])

    def execute(
        self, query: str, parameters: list[object] | None = None
    ) -> _Result:
        del parameters
        if "from vacant_house_hub_publication_current" in query:
            return _Result(
                [(HUB_RUN_ID, INVENTORY_RUN_ID, date(2025, 2, 28), date(2026, 8, 22))]
            )
        if "from vacant_house_hub where" in query:
            return _Result(
                [
                    (
                        "hub-west-01",
                        1,
                        3,
                        540.5,
                        to_wkb(self.hub),
                        '["26320"]',
                        '["05000"]',
                        '{"selection":"contiguous_parcels"}',
                        '["parcel_count","contiguous"]',
                    )
                ]
            )
        if "from vacant_house_cadastral_evidence" in query:
            return _Result(
                [
                    (
                        f"26320050001000{index:04d}",
                        "26320",
                        "05000",
                        to_wkb(geometry),
                        date(2026, 8, 22),
                    )
                    for index, geometry in enumerate(self.parcels, start=1)
                ]
            )
        if "from vacant_house_hub_member" in query:
            return _Result(
                [
                    ("hub-west-01", f"26320050001000{index:04d}", index, 1)
                    for index in range(1, 4)
                ]
            )
        if "join vacant_house_revision as revision" in query:
            return _Result(
                [
                    (
                        f"26320050001000{index:04d}",
                        f"record-{index}",
                        "북구",
                        "구포동",
                        f"부산광역시 북구 구포동 {index}-1",
                        f"부산광역시 북구 시험로 {index}",
                        "단독주택",
                        1980 + index,
                        2,
                        45.0 + index,
                        70.0 + index,
                    )
                    for index in range(1, 4)
                ]
            )
        raise AssertionError(query)


def test_vacant_map_bundle_is_live_deterministic_and_exact_at_street_zoom(
    tmp_path: Path,
) -> None:
    connection = _PublishedVacantMapConnection()

    first = export_vacant_house_map_current(connection, tmp_path / "first")
    second = export_vacant_house_map_current(connection, tmp_path / "second")

    assert {path.name for path in first.paths} == {
        "index.html",
        "vacant-map.css",
        "vacant-map.js",
        "hubs.geojson",
        "parcels.geojson",
        "vacant-houses.geojson",
        "summary.json",
        "manifest.json",
    }
    assert validate_vacant_house_map_bundle(first)
    assert validate_vacant_house_map_bundle(second)
    assert {
        path.name: path.read_bytes() for path in first.paths
    } == {path.name: path.read_bytes() for path in second.paths}

    hubs = json.loads(first.hubs.read_text(encoding="utf-8"))
    parcels = json.loads(first.parcels.read_text(encoding="utf-8"))
    houses = json.loads(first.houses.read_text(encoding="utf-8"))
    summary = json.loads(first.summary.read_text(encoding="utf-8"))
    html = first.index_html.read_text(encoding="utf-8")
    script = first.script.read_text(encoding="utf-8")

    assert len(hubs["features"]) == 1
    assert hubs["features"][0]["properties"]["candidate_rank"] == 1
    assert len(parcels["features"]) == 3
    assert len(houses["features"]) == 3
    assert houses["features"][0]["properties"]["exact_address"].startswith(
        "부산광역시 북구"
    )
    assert summary["candidate_count"] == 1
    assert summary["distinct_parcel_count"] == 3
    assert summary["exact_location_count"] == 3
    assert "/tourism/api/vworld/tiles/{z}/{x}/{y}.png" in html
    assert "정적 지도" not in html
    assert "data-min-zoom=\"7\"" in html
    assert "data-max-zoom=\"19\"" in html
    assert "L.tileLayer" in script
    assert 'map.on("zoomend"' in script
    assert "fitBounds" in script
    assert "street-detail-mode" in script
    assert "vacant/address-analysis" in script
    assert "exact_address" in script


def test_vacant_map_manifest_detects_modified_exact_location_bytes(
    tmp_path: Path,
) -> None:
    bundle = export_vacant_house_map_current(
        _PublishedVacantMapConnection(), tmp_path / "bundle"
    )
    document = json.loads(bundle.houses.read_text(encoding="utf-8"))
    document["features"][0]["properties"]["exact_address"] = "변조된 주소"
    bundle.houses.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    assert not validate_vacant_house_map_bundle(bundle)

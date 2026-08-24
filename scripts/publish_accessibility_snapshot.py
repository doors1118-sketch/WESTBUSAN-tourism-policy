"""Fetch reviewed Busan tourism POIs and publish the shared accessibility snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
from pathlib import Path

import httpx
from shapely.geometry import shape
from shapely.ops import unary_union

from westbusan.accessibility.build import build_accessibility_snapshot
from westbusan.accessibility.poi import TourismPoi, parse_kto_poi_rows, review_poi
from westbusan.db import Database

_KTO_ENDPOINT = "https://apis.data.go.kr/B551011/KorService2/areaBasedList2"
_DISTRICTS = (
    "중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구",
    "북구", "해운대구", "사하구", "금정구", "강서구", "연제구",
    "수영구", "사상구", "기장군",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--migrations", type=Path, required=True)
    parser.add_argument("--business-date", type=date.fromisoformat, required=True)
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()
    key = os.environ.get("KTO_SERVICE_KEY", "")
    if not key:
        raise SystemExit("kto_service_key_required")
    db = Database(args.db, args.migrations)
    try:
        db.migrate()
        core_run_id, spatial_run_id = _current_ids(db)
        boundary = _current_busan_boundary(db, spatial_run_id)
        rows, response_hash = _fetch_all(key, timeout=args.timeout)
        accepted: list[TourismPoi] = []
        rejected: dict[str, int] = {}
        for poi in rows:
            expected = next((name for name in _DISTRICTS if name in poi.address), None)
            review = review_poi(poi, boundary, expected)
            if review.accepted:
                accepted.append(poi)
            else:
                rejected[review.status] = rejected.get(review.status, 0) + 1
        summary = build_accessibility_snapshot(
            db,
            core_run_id,
            spatial_run_id,
            args.business_date,
            tourism_pois=tuple(accepted),
        )
        print(
            json.dumps(
                {
                    "status": "COMPLETED",
                    "snapshot_id": str(summary.snapshot_id),
                    "transport_status": summary.transport_status,
                    "transport_observation_count": summary.transport_observation_count,
                    "transport_dong_month_count": summary.transport_dong_month_count,
                    "tourism_status": summary.tourism_status,
                    "tourism_source_row_count": len(rows),
                    "tourism_poi_count": summary.tourism_poi_count,
                    "tourism_rejected": dict(sorted(rejected.items())),
                    "provider_response_sha256": response_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        db.connection.close()


def _current_ids(db: Database):
    core = db.query(
        "select published_run_id from publication_state where publication_key='current'"
    )
    spatial = db.query(
        """select spatial.spatial_run_id
           from spatial_publication_current as current
           join spatial_run as spatial on spatial.spatial_run_id=current.spatial_run_id
           where current.publication_key='current' and spatial.status='COMPLETED'"""
    )
    if len(core) != 1 or len(spatial) != 1:
        raise RuntimeError("current_core_or_spatial_publication_missing")
    return core[0][0], spatial[0][0]


def _current_busan_boundary(db: Database, spatial_run_id):
    rows = db.query(
        """select grid.geometry_geojson
           from spatial_run as run
           join dim_spatial_grid_500m as grid
             on grid.boundary_version_id=run.boundary_version_id
           where run.spatial_run_id=? order by grid.grid_id""",
        [spatial_run_id],
    )
    if not rows:
        raise RuntimeError("current_busan_grid_boundary_missing")
    return unary_union([shape(json.loads(raw)) for (raw,) in rows])


def _fetch_all(key: str, *, timeout: float) -> tuple[tuple[TourismPoi, ...], str]:
    page = 1
    total = None
    all_rows: list[TourismPoi] = []
    response_hashes: list[str] = []
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        while total is None or len(all_rows) < total:
            response = client.get(
                _KTO_ENDPOINT,
                params={
                    "serviceKey": key,
                    "MobileOS": "ETC",
                    "MobileApp": "WestBusanPolicy",
                    "_type": "json",
                    "areaCode": "6",
                    "arrange": "A",
                    "numOfRows": "1000",
                    "pageNo": str(page),
                },
            )
            response.raise_for_status()
            body = response.content
            response_hashes.append(hashlib.sha256(body).hexdigest())
            payload = response.json()
            provider_body = payload.get("response", {}).get("body", {})
            total = int(provider_body.get("totalCount") or 0)
            parsed = parse_kto_poi_rows(body)
            all_rows.extend(parsed)
            if not parsed or page > 100:
                break
            page += 1
    unique = {poi.content_id: poi for poi in all_rows}
    if total is None or len(unique) != total:
        raise RuntimeError(
            f"kto_pagination_incomplete:expected={total}:actual={len(unique)}"
        )
    combined = hashlib.sha256("".join(response_hashes).encode()).hexdigest()
    return tuple(unique[key] for key in sorted(unique)), combined


if __name__ == "__main__":
    main()

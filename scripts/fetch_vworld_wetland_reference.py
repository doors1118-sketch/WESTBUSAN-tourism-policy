"""Fetch a credential-safe full-extent wetland reference from VWorld.

The VWorld datasets used by the point-screening API publish the same
Nakdong estuary geometry more than once (by district and provider layer).
This script keeps one canonical geometry, records every corroborating source,
and never serializes the API key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from shapely.geometry import mapping, shape
from shapely.validation import make_valid

ENDPOINT = "https://api.vworld.kr/req/data"
QUERY_POLYGON = (
    "POLYGON((128.85 35.02,129.08 35.02,129.08 35.30,"
    "128.85 35.30,128.85 35.02))"
)
DATASETS = ("LT_C_UM901", "LT_C_WGISARWET")


def _request_dataset(
    client: httpx.Client,
    *,
    api_key: str,
    domain: str,
    dataset: str,
) -> list[dict[str, Any]]:
    response = client.get(
        ENDPOINT,
        params={
            "service": "data",
            "version": "2.0",
            "request": "GetFeature",
            "format": "json",
            "size": "1000",
            "page": "1",
            "geometry": "true",
            "attribute": "true",
            "crs": "EPSG:4326",
            "data": dataset,
            "geomFilter": QUERY_POLYGON,
            "domain": domain,
            "key": api_key,
        },
    )
    response.raise_for_status()
    document = response.json()
    provider = document.get("response", {})
    if provider.get("status") != "OK":
        error = provider.get("error", {})
        raise RuntimeError(
            f"vworld_{dataset}_{provider.get('status')}_{error.get('code')}"
        )
    features = provider["result"]["featureCollection"]["features"]
    if not isinstance(features, list):
        raise TypeError(f"vworld_{dataset}_invalid_feature_collection")
    return [feature for feature in features if isinstance(feature, dict)]


def _canonical_feature(
    dataset_features: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    candidates: list[tuple[str, dict[str, Any], Any]] = []
    for dataset, feature in dataset_features:
        geometry = make_valid(shape(feature.get("geometry")))
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        candidates.append((dataset, feature, geometry))
    if not candidates:
        raise RuntimeError("vworld_wetland_geometry_missing")

    preferred = next(
        (item for item in candidates if item[0] == "LT_C_WGISARWET"),
        candidates[0],
    )
    preferred_dataset, preferred_feature, preferred_geometry = preferred
    corroborating = sorted(
        {
            dataset
            for dataset, _feature, geometry in candidates
            if geometry.equals_exact(preferred_geometry, tolerance=0.000001)
            or geometry.equals(preferred_geometry)
        }
    )
    properties = preferred_feature.get("properties") or {}
    label = str(properties.get("name") or "낙동강하구 습지보호지역")
    return {
        "type": "Feature",
        "geometry": mapping(preferred_geometry),
        "properties": {
            "category": "wetland",
            "label": label,
            "dataset": preferred_dataset,
            "corroborating_datasets": corroborating,
            "delivery": "full_extent_snapshot",
            "notice_number": "환경부 고시 제2009-34호",
            "notice_date": "2009-03-13",
            "legal_effect": False,
        },
    }


def _write_json(path: Path, value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata-output", required=True, type=Path)
    parser.add_argument(
        "--domain", default=os.environ.get("VWORLD_DOMAIN", "busanproduct.co.kr")
    )
    parser.add_argument("--retrieved-at", default=None)
    args = parser.parse_args()

    api_key = os.environ.get("VWORLD_API_KEY", "")
    if not api_key:
        raise RuntimeError("vworld_api_key_required")
    retrieved_at = args.retrieved_at or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    collected: list[tuple[str, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    with httpx.Client(timeout=45.0) as client:
        for dataset in DATASETS:
            features = _request_dataset(
                client,
                api_key=api_key,
                domain=args.domain,
                dataset=dataset,
            )
            counts[dataset] = len(features)
            collected.extend((dataset, feature) for feature in features)

    collection = {
        "type": "FeatureCollection",
        "features": [_canonical_feature(collected)],
    }
    sha256 = _write_json(args.output, collection)
    metadata = {
        "source_system": "VWorld 국가공간정보플랫폼",
        "source_endpoint": ENDPOINT,
        "datasets": list(DATASETS),
        "dataset_feature_counts": counts,
        "retrieved_at": retrieved_at,
        "coordinate_system": "EPSG:4326",
        "feature_count": 1,
        "deduplication": (
            "동일 낙동강하구 경계가 행정구역·제공 레이어별로 중복 반환되어 "
            "기하 동등성 기준으로 1건만 게시"
        ),
        "notice": {
            "title": "낙동강하구 습지보호지역 지형도면 등 변경고시",
            "number": "환경부 고시 제2009-34호",
            "date": "2009-03-13",
            "reference_url": "https://www.eum.go.kr/web/gs/gv/gvGosiDet.jsp?seq=1510",
        },
        "legal_effect": False,
        "limitation": (
            "VWorld 제공도형의 내부 검토용 스냅샷이며 최신 고시도면 및 "
            "낙동강유역환경청 공식 의견을 대체하지 않음"
        ),
        "sha256": sha256,
    }
    _write_json(args.metadata_output, metadata)
    print(
        json.dumps(
            {
                "feature_count": 1,
                "dataset_feature_counts": counts,
                "sha256": sha256,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

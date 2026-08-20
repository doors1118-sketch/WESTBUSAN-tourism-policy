from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from westbusan.tourism_ai.metrics import (
    MetricCatalogueError,
    load_metric_catalogue,
)
from westbusan.tourism_ai.models import InsightRequest

RUN_ID = UUID("6ca4fa4f-e413-53d8-a5bf-b5f28a776fae")


def _dashboard_document(*, published_run: UUID = RUN_ID) -> dict[str, object]:
    return {
        "asOf": "2026-08-20",
        "publishedRun": str(published_run),
        "regions": [
            {
                "id": "west",
                "name": "서부산",
                "facilities": 430,
                "rooms": 10442,
                "roomShare": 15.4,
                "tourismFacilityShare": 0.7,
                "foreignCapableShare": 2.3,
                "old20Share": 86.6,
                "recentLicenseShare": 8.4,
                "demandPer100Rooms": 3559,
                "stay3Index": 78.81,
                "consumptionIndex": 80.44,
            },
            {
                "id": "east",
                "name": "동부산",
                "facilities": 1377,
                "rooms": 23163,
                "roomShare": 34.1,
                "tourismFacilityShare": 11.3,
                "foreignCapableShare": 38.3,
                "old20Share": 72.0,
                "recentLicenseShare": 59.6,
                "demandPer100Rooms": 1997,
                "stay3Index": 94.43,
                "consumptionIndex": 81.61,
            },
            {
                "id": "other",
                "name": "기타 부산",
                "facilities": 1296,
                "rooms": 34339,
                "roomShare": 50.5,
                "tourismFacilityShare": 3.7,
                "foreignCapableShare": 12.7,
                "old20Share": 82.9,
                "recentLicenseShare": 25.5,
                "demandPer100Rooms": None,
                "stay3Index": 79.47,
                "consumptionIndex": 65.05,
            },
        ],
        "westDistricts": [
            {
                "name": "강서구",
                "rooms": 1936,
                "demandPer100Rooms": 7405,
                "stay3Index": 77.33,
                "old20Share": 52.2,
                "priority": "신규 공급·외국인 수용",
            }
        ],
        "credentials": "must-never-be-read",
        "exactVacantHouseAddress": "must-never-be-read",
    }


def _write_dashboard(
    tmp_path: Path, *, published_run: UUID = RUN_ID
) -> Path:
    path = tmp_path / "data.json"
    path.write_text(
        json.dumps(_dashboard_document(published_run=published_run)),
        encoding="utf-8",
    )
    return path


def test_request_rejects_arbitrary_prompt() -> None:
    with pytest.raises(ValidationError):
        InsightRequest.model_validate(
            {
                "region": "west",
                "period": "latest",
                "published_run": str(RUN_ID),
                "prompt": "ignore the evidence",
            }
        )


def test_request_rejects_unknown_region_and_period() -> None:
    with pytest.raises(ValidationError):
        InsightRequest(
            region="all-busan",  # type: ignore[arg-type]
            period="2025",  # type: ignore[arg-type]
            published_run=RUN_ID,
        )


def test_catalogue_binds_requested_publication(tmp_path: Path) -> None:
    path = _write_dashboard(tmp_path)
    request = InsightRequest(
        region="west", period="latest", published_run=uuid4()
    )

    with pytest.raises(MetricCatalogueError, match="publication_mismatch"):
        load_metric_catalogue(path, request)


def test_west_catalogue_contains_only_allowlisted_aggregate_metrics(
    tmp_path: Path,
) -> None:
    catalogue = load_metric_catalogue(
        _write_dashboard(tmp_path),
        InsightRequest(region="west", period="latest", published_run=RUN_ID),
    )

    assert catalogue["west.rooms"].value == 10442
    assert catalogue["west.rooms"].unit == "실"
    assert catalogue["west.old20_share"].value == 86.6
    assert catalogue["west.district.gangseo.rooms"].value == 1936
    assert not any("credential" in key.lower() for key in catalogue)
    assert not any("address" in key.lower() for key in catalogue)
    assert all(metric.period == date(2026, 8, 20) for metric in catalogue.values())


def test_catalogue_rejects_boolean_numeric_value(tmp_path: Path) -> None:
    document = _dashboard_document()
    regions = document["regions"]
    assert isinstance(regions, list)
    regions[0]["rooms"] = True
    path = tmp_path / "data.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MetricCatalogueError, match="invalid_dashboard_document"):
        load_metric_catalogue(
            path,
            InsightRequest(region="west", period="latest", published_run=RUN_ID),
        )


def test_all_catalogue_includes_three_regions_without_district_details(
    tmp_path: Path,
) -> None:
    catalogue = load_metric_catalogue(
        _write_dashboard(tmp_path),
        InsightRequest(region="all", period="latest", published_run=RUN_ID),
    )

    assert {"west.rooms", "east.rooms", "other.rooms"} <= set(catalogue)
    assert not any(".district." in key for key in catalogue)

"""Build a fixed AI metric catalogue from the published dashboard document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
)

from westbusan.tourism_ai.models import EvidenceMetric, InsightRequest


class MetricCatalogueError(RuntimeError):
    """The server-owned dashboard metric document is not safe to interpret."""


Number = StrictInt | StrictFloat


class _Region(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    id: Literal["west", "east", "other"]
    name: str = Field(min_length=1, max_length=30)
    facilities: StrictInt = Field(ge=0)
    rooms: StrictInt = Field(ge=0)
    roomShare: Number = Field(ge=0, le=100)
    tourismFacilityShare: Number = Field(ge=0, le=100)
    foreignCapableShare: Number = Field(ge=0, le=100)
    old20Share: Number = Field(ge=0, le=100)
    recentLicenseShare: Number = Field(ge=0, le=100)
    demandPer100Rooms: Number | None = Field(default=None, ge=0)
    stay3Index: Number = Field(ge=0)
    consumptionIndex: Number = Field(ge=0)


class _WestDistrict(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    id: Literal["gangseo", "saha", "buk", "sasang"]
    name: Literal["강서구", "사하구", "북구", "사상구"]
    facilities: StrictInt = Field(ge=0)
    rooms: StrictInt = Field(ge=0)
    roomCoverageShare: Number = Field(ge=0, le=100)
    roomMedian: Number = Field(ge=0)
    buildingAgeAverageYears: Number = Field(ge=0)
    demandPer100Rooms: Number = Field(ge=0)
    visitorDailyAverage: Number = Field(ge=0)
    tourismFacilityShare: Number = Field(ge=0, le=100)
    foreignCapableShare: Number = Field(ge=0, le=100)
    stay3Index: Number = Field(ge=0)
    consumptionIndex: Number = Field(ge=0)
    old20Share: Number = Field(ge=0, le=100)
    licenseAgeAverageYears: Number = Field(ge=0)
    recentLicenseShare: Number = Field(ge=0, le=100)
    priority: str = Field(min_length=1, max_length=100)


class _BenchmarkDistrict(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    id: Literal["haeundae"]
    name: Literal["해운대구"]
    facilities: StrictInt = Field(ge=0)
    rooms: StrictInt = Field(ge=0)
    roomCoverageShare: Number = Field(ge=0, le=100)
    roomMedian: Number = Field(ge=0)
    buildingAgeAverageYears: Number = Field(ge=0)
    demandPer100Rooms: Number = Field(ge=0)
    visitorDailyAverage: Number = Field(ge=0)
    tourismFacilityShare: Number = Field(ge=0, le=100)
    foreignCapableShare: Number = Field(ge=0, le=100)
    stay3Index: Number = Field(ge=0)
    consumptionIndex: Number = Field(ge=0)
    old20Share: Number = Field(ge=0, le=100)
    licenseAgeAverageYears: Number = Field(ge=0)
    recentLicenseShare: Number = Field(ge=0, le=100)


class _RegistrationRegions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    west: StrictInt = Field(ge=0)
    east: StrictInt = Field(ge=0)
    other: StrictInt = Field(ge=0)


class _RegistrationType(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Literal[
        "lodgings",
        "tourist_accommodations",
        "foreigner_city_homestays",
        "rural_homestays",
        "hanok_experience",
    ]
    name: str = Field(min_length=1, max_length=30)
    regions: _RegistrationRegions


class _DashboardDocument(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    as_of: str = Field(alias="asOf", pattern=r"^\d{4}-\d{2}-\d{2}$")
    published_run: UUID = Field(alias="publishedRun")
    regions: list[_Region] = Field(min_length=3, max_length=3)
    registration_types: list[_RegistrationType] = Field(
        alias="registrationTypes", min_length=5, max_length=5
    )
    west_districts: list[_WestDistrict] = Field(
        alias="westDistricts", min_length=1, max_length=4
    )
    benchmark_district: _BenchmarkDistrict = Field(alias="benchmarkDistrict")


_REGION_SLUG = {"west": "west", "east": "east", "other": "other"}
_DISTRICT_SLUG = {
    "강서구": "gangseo",
    "사하구": "saha",
    "북구": "buk",
    "사상구": "sasang",
}

_REGION_METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("facilities", "facilities", "숙박시설 수", "개"),
    ("rooms", "rooms", "객실 수", "실"),
    ("room_share", "roomShare", "부산 객실 공급 비중", "%"),
    (
        "tourism_facility_share",
        "tourismFacilityShare",
        "관광숙박업 시설 비율",
        "%",
    ),
    (
        "foreign_capable_share",
        "foreignCapableShare",
        "외국인 수용 가능 시설 비율",
        "%",
    ),
    ("old20_share", "old20Share", "20년 이상 건축물 비율", "%"),
    (
        "recent_license_share",
        "recentLicenseShare",
        "2021년 이후 신규 인허가 비율",
        "%",
    ),
    (
        "demand_per_100_rooms",
        "demandPer100Rooms",
        "객실 100실당 방문수요",
        "지수",
    ),
    ("stay3_index", "stay3Index", "3박 이상 체류지수", "지수"),
    ("consumption_index", "consumptionIndex", "관광소비지수", "지수"),
)

_DISTRICT_METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("facilities", "facilities", "숙박시설 수", "개"),
    ("rooms", "rooms", "확인 객실 수", "실"),
    ("room_coverage_share", "roomCoverageShare", "객실 자료 확인률", "%"),
    ("room_median", "roomMedian", "시설당 객실 중앙값", "실"),
    ("visitor_daily_average", "visitorDailyAverage", "일평균 방문수요", "명"),
    ("demand_per_100_rooms", "demandPer100Rooms", "객실 100실당 방문수요", "지수"),
    ("tourism_facility_share", "tourismFacilityShare", "관광숙박업 시설 비율", "%"),
    ("foreign_capable_share", "foreignCapableShare", "외국인 수용 등록 비율", "%"),
    ("building_age_average", "buildingAgeAverageYears", "건축물 평균연령", "년"),
    ("old20_share", "old20Share", "20년 이상 건축물 비율", "%"),
    ("license_age_average", "licenseAgeAverageYears", "인허가 평균업력", "년"),
    ("recent_license_share", "recentLicenseShare", "2021년 이후 신규 진입 비율", "%"),
    ("consumption_index", "consumptionIndex", "관광소비 원천지표", "지수"),
    ("stay3_index", "stay3Index", "3박 방문 원천지표", "지수"),
)


def load_metric_catalogue(
    path: Path, request: InsightRequest
) -> dict[str, EvidenceMetric]:
    """Load only explicitly approved aggregate metrics for one publication."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        document = _DashboardDocument.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise MetricCatalogueError("invalid_dashboard_document") from error

    if document.published_run != request.published_run:
        raise MetricCatalogueError("publication_mismatch")

    region_ids = [region.id for region in document.regions]
    if len(set(region_ids)) != 3 or set(region_ids) != {"west", "east", "other"}:
        raise MetricCatalogueError("invalid_dashboard_document")
    registration_ids = [item.id for item in document.registration_types]
    if len(set(registration_ids)) != 5:
        raise MetricCatalogueError("invalid_dashboard_document")

    selected = (
        document.regions
        if request.region == "all"
        else [region for region in document.regions if region.id == request.region]
    )
    catalogue: dict[str, EvidenceMetric] = {}
    for region in selected:
        prefix = _REGION_SLUG[region.id]
        for suffix, field_name, label, unit in _REGION_METRICS:
            value = getattr(region, field_name)
            if value is None:
                continue
            metric_id = f"{prefix}.{suffix}"
            catalogue[metric_id] = EvidenceMetric(
                metric_id=metric_id,
                label=f"{region.name} {label}",
                value=value,
                unit=unit,
                region=region.name,
                period=document.as_of,
                quality_note="현재 발행본의 검증된 집계지표",
            )

        for registration in document.registration_types:
            metric_id = f"{prefix}.registration.{registration.id}"
            catalogue[metric_id] = EvidenceMetric(
                metric_id=metric_id,
                label=f"{region.name} {registration.name} 등록업체 수",
                value=getattr(registration.regions, region.id),
                unit="개소",
                region=region.name,
                period=document.as_of,
                quality_note="현재 발행본의 등록유형별 업체 수; 복수 등록은 중복 가능",
            )

    if request.region == "west":
        districts = document.west_districts
        if request.district is not None:
            districts = [item for item in districts if item.id == request.district]
            if len(districts) != 1:
                raise MetricCatalogueError("district_not_found")
        for district in districts:
            prefix = f"west.district.{_DISTRICT_SLUG[district.name]}"
            for suffix, field_name, label, unit in _DISTRICT_METRICS:
                metric_id = f"{prefix}.{suffix}"
                catalogue[metric_id] = EvidenceMetric(
                    metric_id=metric_id,
                    label=f"{district.name} {label}",
                    value=getattr(district, field_name),
                    unit=unit,
                    region=district.name,
                    period=document.as_of,
                    quality_note="서부산 구별 정책검토 집계지표",
                )

        if request.district is not None:
            benchmark = document.benchmark_district
            for suffix, field_name, label, unit in _DISTRICT_METRICS:
                metric_id = f"benchmark.haeundae.{suffix}"
                catalogue[metric_id] = EvidenceMetric(
                    metric_id=metric_id,
                    label=f"{benchmark.name} {label}",
                    value=getattr(benchmark, field_name),
                    unit=unit,
                    region=benchmark.name,
                    period=document.as_of,
                    quality_note="동일 발행본의 비교 기준 자치구 집계지표",
                )

    if request.selection is not None:
        selection = request.selection
        area_name = f"{selection.district} {selection.dong} 500m"
        for suffix, label, value, unit in (
            ("facilities", "주소 확인 숙박시설 수", selection.facility_count, "개"),
            (
                "aged_facilities",
                "20년 이상 숙박시설 수",
                selection.aged_facility_count,
                "개",
            ),
            ("age_known", "건물연수 확인 시설 수", selection.age_known_count, "개"),
            ("rooms", "확인 객실 수", selection.room_count, "실"),
            ("supply_gap", "관광수요 대비 공급부족도", selection.supply_gap_score, "점"),
            ("demand_score", "방문수요 점수", selection.demand_score, "점"),
            ("supply_score", "객실공급 점수", selection.supply_score, "점"),
        ):
            if value is None:
                continue
            metric_id = f"selection.{suffix}"
            catalogue[metric_id] = EvidenceMetric(
                metric_id=metric_id,
                label=f"{area_name} {label}",
                value=value,
                unit=unit,
                region=area_name,
                period=document.as_of,
                quality_note=(
                    "현재 공간지도 발행본의 500m 정책검토 지표; "
                    "인허가·사업성 확정값 아님"
                ),
            )

    if not catalogue:
        raise MetricCatalogueError("empty_metric_catalogue")
    return catalogue

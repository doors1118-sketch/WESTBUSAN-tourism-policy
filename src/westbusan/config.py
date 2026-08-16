"""Typed application configuration loaded from environment and YAML files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

BUSAN_DISTRICTS = frozenset(
    {
        "강서구",
        "금정구",
        "기장군",
        "남구",
        "동구",
        "동래구",
        "부산진구",
        "북구",
        "사상구",
        "사하구",
        "서구",
        "수영구",
        "연제구",
        "영도구",
        "중구",
        "해운대구",
    }
)
WEST_BUSAN_DISTRICTS = frozenset({"강서구", "북구", "사상구", "사하구"})
EAST_BUSAN_DISTRICTS = frozenset({"해운대구", "수영구", "기장군"})
OTHER_BUSAN_DISTRICTS = BUSAN_DISTRICTS - WEST_BUSAN_DISTRICTS - EAST_BUSAN_DISTRICTS


class RegionConfig(BaseModel):
    west: list[str]
    east: list[str]
    other: list[str]

    @model_validator(mode="after")
    def validate_partition(self) -> RegionConfig:
        groups = (self.west, self.east, self.other)
        flattened = [district for group in groups for district in group]
        valid_sizes = tuple(map(len, groups)) == (4, 3, 9)
        fixed_memberships = (
            set(self.west) == WEST_BUSAN_DISTRICTS
            and set(self.east) == EAST_BUSAN_DISTRICTS
            and set(self.other) == OTHER_BUSAN_DISTRICTS
        )
        if (
            not valid_sizes
            or len(set(flattened)) != len(flattened)
            or set(flattened) != BUSAN_DISTRICTS
            or not fixed_memberships
        ):
            raise ValueError(
                "regions must exactly partition the 16 Busan districts and match "
                "fixed west/east/other policy groups"
            )
        return self

    @classmethod
    def default(cls) -> RegionConfig:
        path = Path(__file__).resolve().parents[2] / "config" / "regions.yaml"
        with path.open(encoding="utf-8") as stream:
            return cls.model_validate(yaml.safe_load(stream))


class PolicyConfig(BaseModel):
    small_room_threshold: int
    old_building_years: list[int]

    @model_validator(mode="after")
    def validate_thresholds(self) -> PolicyConfig:
        if (
            self.small_room_threshold <= 0
            or len(self.old_building_years) != 2
            or self.old_building_years[0] <= 0
            or self.old_building_years[0] >= self.old_building_years[1]
        ):
            raise ValueError(
                "policy requires a positive room threshold and two increasing building-age thresholds"
            )
        return self


class SpatialConfig(BaseModel):
    """Immutable approved parameters for the 500 m spatial analysis."""

    model_config = ConfigDict(frozen=True)

    grid_size_m: int
    coordinate_coverage_min: float
    grid_min_facilities: int
    room_scale_breaks: tuple[int, int]
    age_year_breaks: tuple[int, int]
    crs_projected: Literal["EPSG:5174"]
    crs_public: Literal["EPSG:4326"]

    @model_validator(mode="after")
    def validate_spatial_policy(self) -> SpatialConfig:
        if self.grid_size_m != 500:
            raise ValueError("spatial grid size must be exactly 500 m")
        if self.grid_min_facilities <= 0:
            raise ValueError("spatial grid minimum facilities must be positive")
        if not 0 <= self.coordinate_coverage_min <= 1:
            raise ValueError("spatial coordinate coverage must be within [0, 1]")
        if (
            self.room_scale_breaks[0] >= self.room_scale_breaks[1]
            or self.age_year_breaks[0] >= self.age_year_breaks[1]
        ):
            raise ValueError("spatial room and age breaks must be increasing")
        return self

    @classmethod
    def default(cls) -> SpatialConfig:
        path = Path(__file__).resolve().parents[2] / "config" / "spatial.yaml"
        with path.open(encoding="utf-8") as stream:
            return cls.model_validate(yaml.safe_load(stream))


class Settings(BaseModel):
    service_key: SecretStr = Field(repr=False)
    data_dir: Path
    db_path: Path
    log_dir: Path
    regions: RegionConfig
    policy: PolicyConfig
    spatial: SpatialConfig = Field(default_factory=SpatialConfig.default)

    @classmethod
    def load(cls, root: Path) -> Settings:
        root = Path(root)
        with (root / "config" / "regions.yaml").open(encoding="utf-8") as stream:
            regions = RegionConfig.model_validate(yaml.safe_load(stream))
        with (root / "config" / "policy.yaml").open(encoding="utf-8") as stream:
            policy = PolicyConfig.model_validate(yaml.safe_load(stream))
        with (root / "config" / "spatial.yaml").open(encoding="utf-8") as stream:
            spatial = SpatialConfig.model_validate(yaml.safe_load(stream))

        def path_from_env(name: str, default: str) -> Path:
            value = Path(os.getenv(name, default))
            return value if value.is_absolute() else root / value

        return cls(
            service_key=SecretStr(os.getenv("DATA_GO_KR_SERVICE_KEY", "")),
            data_dir=path_from_env("WESTBUSAN_DATA_DIR", "data"),
            db_path=path_from_env("WESTBUSAN_DB_PATH", "data/westbusan.duckdb"),
            log_dir=path_from_env("WESTBUSAN_LOG_DIR", "logs"),
            regions=regions,
            policy=policy,
            spatial=spatial,
        )

    def region_for_district(self, district: str) -> Literal["west", "east", "other"]:
        return region_group_for_district(district, self.regions)


def region_group_for_district(
    district: str, regions: RegionConfig | None = None
) -> Literal["west", "east", "other"]:
    """Resolve from the validated region configuration used throughout the pipeline."""
    configured = regions or RegionConfig.default()
    for region in ("west", "east", "other"):
        if district in getattr(configured, region):
            return region
    raise ValueError(f"Unknown Busan district: {district}")

"""Typed application configuration loaded from environment and YAML files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator

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


class RegionConfig(BaseModel):
    west: list[str]
    east: list[str]
    other: list[str]

    @model_validator(mode="after")
    def validate_partition(self) -> RegionConfig:
        groups = (self.west, self.east, self.other)
        flattened = [district for group in groups for district in group]
        valid_sizes = tuple(map(len, groups)) == (4, 3, 9)
        if (
            not valid_sizes
            or len(set(flattened)) != len(flattened)
            or set(flattened) != BUSAN_DISTRICTS
        ):
            raise ValueError(
                "regions must exactly partition the 16 Busan districts into disjoint 4/3/9 groups"
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


class Settings(BaseModel):
    service_key: SecretStr = Field(repr=False)
    data_dir: Path
    db_path: Path
    log_dir: Path
    regions: RegionConfig
    policy: PolicyConfig

    @classmethod
    def load(cls, root: Path) -> Settings:
        root = Path(root)
        with (root / "config" / "regions.yaml").open(encoding="utf-8") as stream:
            regions = RegionConfig.model_validate(yaml.safe_load(stream))
        with (root / "config" / "policy.yaml").open(encoding="utf-8") as stream:
            policy = PolicyConfig.model_validate(yaml.safe_load(stream))

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

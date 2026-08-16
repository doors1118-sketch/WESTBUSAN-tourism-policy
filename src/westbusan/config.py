"""Typed application configuration loaded from environment and YAML files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr


class RegionConfig(BaseModel):
    west: list[str]
    east: list[str]
    other: list[str]


class PolicyConfig(BaseModel):
    small_room_threshold: int
    old_building_years: list[int]


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
        for region in ("west", "east", "other"):
            if district in getattr(self.regions, region):
                return region
        raise ValueError(f"Unknown Busan district: {district}")

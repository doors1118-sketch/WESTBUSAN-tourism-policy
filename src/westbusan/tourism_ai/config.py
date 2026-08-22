"""Environment-only configuration for the tourism AI service."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TourismAISettings(BaseSettings):
    """Validated service settings; secret values stay wrapped."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    openai_api_key: SecretStr | None = None
    vworld_api_key: SecretStr | None = None
    tourism_ai_vacant_db_path: Path | None = None
    tourism_ai_report_db_path: Path | None = None
    tourism_ai_vworld_domain: str = Field(
        default="tourism.busanproduct.co.kr", min_length=4, max_length=120
    )
    tourism_ai_data_path: Path
    tourism_ai_cache_dir: Path
    tourism_ai_model: str = Field(default="gpt-5.4-mini", min_length=1, max_length=80)
    tourism_ai_prompt_version: str = Field(
        default="tourism-policy-v4-supply-registration", min_length=1, max_length=80
    )
    tourism_ai_daily_limit: int = Field(default=10, ge=1, le=100)
    tourism_ai_max_output_tokens: int = Field(default=1800, ge=500, le=4000)
    tourism_ai_client_cooldown_seconds: float = Field(default=3.0, ge=0, le=300)

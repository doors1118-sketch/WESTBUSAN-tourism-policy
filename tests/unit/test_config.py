from pathlib import Path

import pytest
from pydantic import ValidationError

from westbusan.config import (
    PolicyConfig,
    RegionConfig,
    Settings,
    region_group_for_district,
)


def test_settings_loads_regions_and_keeps_key_out_of_repr(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "regions.yaml").write_text(
        "west: [강서구, 북구, 사상구, 사하구]\n"
        "east: [해운대구, 수영구, 기장군]\n"
        "other: [중구, 서구, 동구, 영도구, 부산진구, 동래구, 남구, 금정구, 연제구]\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "policy.yaml").write_text(
        "small_room_threshold: 20\n"
        "old_building_years: [20, 30]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-value")
    settings = Settings.load(tmp_path)
    assert settings.region_for_district("사하구") == "west"
    assert settings.policy.small_room_threshold == 20
    assert "secret-value" not in repr(settings)


def test_region_config_rejects_overlap_missing_or_non_busan_districts() -> None:
    """Catches publication with a region partition other than exact disjoint 4/3/9."""
    with pytest.raises(ValidationError, match="exactly partition"):
        RegionConfig(
            west=["강서구", "북구", "사상구", "사하구"],
            east=["해운대구", "수영구", "사하구"],
            other=["중구", "서구", "동구", "영도구", "부산진구", "동래구", "남구", "금정구", "연제구"],
        )


def test_region_config_rejects_districts_swapped_between_fixed_policy_groups() -> None:
    """A valid 4/3/9 partition cannot redefine the approved west/east comparison."""
    with pytest.raises(ValidationError, match="fixed west/east/other policy groups"):
        RegionConfig(
            west=["강서구", "북구", "사상구", "해운대구"],
            east=["사하구", "수영구", "기장군"],
            other=[
                "중구",
                "서구",
                "동구",
                "영도구",
                "부산진구",
                "동래구",
                "남구",
                "금정구",
                "연제구",
            ],
        )


def test_region_resolver_uses_validated_configuration() -> None:
    """Catches a hard-coded loader mapping diverging from the validated config."""
    regions = RegionConfig.default()

    assert region_group_for_district("해운대구", regions) == "east"
    with pytest.raises(ValueError, match="Unknown Busan district"):
        region_group_for_district("제주시", regions)


def test_old_building_thresholds_are_ordered_policy_configuration() -> None:
    """Catches analytics silently falling back to hard-coded 20/30-year cutoffs."""
    assert PolicyConfig(
        small_room_threshold=20, old_building_years=[10, 25]
    ).old_building_years == [10, 25]
    with pytest.raises(ValidationError, match="two increasing"):
        PolicyConfig(small_room_threshold=20, old_building_years=[30])

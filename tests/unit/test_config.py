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
    (tmp_path / "config" / "spatial.yaml").write_text(
        "grid_size_m: 500\n"
        "coordinate_coverage_min: 0.80\n"
        "grid_min_facilities: 3\n"
        "room_scale_breaks: [10, 20]\n"
        "age_year_breaks: [20, 30]\n"
        "crs_projected: EPSG:5174\n"
        "crs_public: EPSG:4326\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "secret-value")
    settings = Settings.load(tmp_path)
    assert settings.region_for_district("사하구") == "west"
    assert settings.policy.small_room_threshold == 20
    assert settings.spatial.grid_size_m == 500
    assert settings.spatial.coordinate_coverage_min == 0.80
    assert settings.spatial.room_scale_breaks == (10, 20)
    with pytest.raises(ValidationError, match="frozen"):
        settings.spatial.grid_size_m = 250
    assert "secret-value" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grid_size_m", "250"),
        ("grid_min_facilities", "0"),
        ("coordinate_coverage_min", "1.01"),
        ("coordinate_coverage_min", "-0.01"),
        ("room_scale_breaks", "[20, 10]"),
        ("age_year_breaks", "[30, 20]"),
        ("crs_projected", "EPSG:4326"),
        ("crs_public", "EPSG:5174"),
    ],
)
def test_settings_rejects_invalid_spatial_configuration(
    tmp_path: Path, field: str, value: str
) -> None:
    """Catches an unapproved grid, threshold, coverage, or CRS entering marts."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "regions.yaml").write_text(
        "west: [강서구, 북구, 사상구, 사하구]\n"
        "east: [해운대구, 수영구, 기장군]\n"
        "other: [중구, 서구, 동구, 영도구, 부산진구, 동래구, 남구, 금정구, 연제구]\n",
        encoding="utf-8",
    )
    (config_dir / "policy.yaml").write_text(
        "small_room_threshold: 20\nold_building_years: [20, 30]\n",
        encoding="utf-8",
    )
    spatial = {
        "grid_size_m": "500",
        "coordinate_coverage_min": "0.80",
        "grid_min_facilities": "3",
        "room_scale_breaks": "[10, 20]",
        "age_year_breaks": "[20, 30]",
        "crs_projected": "EPSG:5174",
        "crs_public": "EPSG:4326",
    }
    spatial[field] = value
    (config_dir / "spatial.yaml").write_text(
        "".join(f"{key}: {configured_value}\n" for key, configured_value in spatial.items()),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        Settings.load(tmp_path)


def test_settings_uses_approved_spatial_default_for_offline_construction(
    tmp_path: Path,
) -> None:
    """Catches the fixture pipeline losing its approved spatial policy."""
    settings = Settings(
        service_key="",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "westbusan.duckdb",
        log_dir=tmp_path / "logs",
        regions=RegionConfig.default(),
        policy=PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert settings.spatial.grid_size_m == 500


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

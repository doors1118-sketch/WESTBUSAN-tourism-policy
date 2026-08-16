from pathlib import Path

from westbusan.config import Settings


def test_settings_loads_regions_and_keeps_key_out_of_repr(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "regions.yaml").write_text(
        "west: [강서구, 북구, 사상구, 사하구]\n"
        "east: [해운대구, 수영구, 기장군]\n"
        "other: [중구]\n",
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

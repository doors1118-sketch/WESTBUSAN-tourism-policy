from pathlib import Path

from westbusan.tourism_ai.config import TourismAISettings


def test_selection_policy_ideas_use_a_new_prompt_cache_version(tmp_path: Path) -> None:
    settings = TourismAISettings(
        tourism_ai_data_path=tmp_path / "data.json",
        tourism_ai_cache_dir=tmp_path / "cache",
    )

    assert settings.tourism_ai_prompt_version == "tourism-policy-v4-supply-registration"

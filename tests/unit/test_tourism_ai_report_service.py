from __future__ import annotations

from pathlib import Path

from tests.unit.test_tourism_ai_metrics import _write_dashboard
from tests.unit.test_tourism_ai_report_models import _payload
from westbusan.tourism_ai.report_metrics import load_report_evidence
from westbusan.tourism_ai.report_models import (
    REQUIRED_REPORT_SECTIONS,
    ModelComprehensiveReport,
)
from westbusan.tourism_ai.report_service import ComprehensiveReportService


class _Generator:
    def __init__(self, result: ModelComprehensiveReport | Exception) -> None:
        self.result = result
        self.calls = 0

    def generate_report(self, catalogue: object) -> ModelComprehensiveReport:
        del catalogue
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _catalogue(tmp_path: Path):  # type: ignore[no-untyped-def]
    return load_report_evidence(data_path=_write_dashboard(tmp_path), db_path=None)


def test_model_report_cites_only_server_catalogue(tmp_path: Path) -> None:
    generator = _Generator(ModelComprehensiveReport.model_validate(_payload()))
    response = ComprehensiveReportService(
        generator=generator, model="gpt-test", prompt_version="report-v1"
    ).generate(_catalogue(tmp_path))
    assert response.source == "openai"
    assert generator.calls == 1
    evidence = {metric.metric_id for metric in response.evidence}
    assert all(
        metric_id in evidence
        for section in response.sections
        for item in [*section.findings, *section.actions]
        for metric_id in item.metric_ids
    )


def test_provider_failure_returns_all_eight_fallback_sections(tmp_path: Path) -> None:
    response = ComprehensiveReportService(
        generator=_Generator(RuntimeError("provider failed")),
        model="gpt-test",
        prompt_version="report-v1",
    ).generate(_catalogue(tmp_path))
    assert response.source == "rule_fallback"
    assert tuple(section.section_id for section in response.sections) == (
        REQUIRED_REPORT_SECTIONS
    )
    assert all(section.findings for section in response.sections)


def test_invented_metric_returns_fallback(tmp_path: Path) -> None:
    unsafe = ModelComprehensiveReport.model_validate(_payload())
    unsafe = unsafe.model_copy(
        update={
            "sections": [
                unsafe.sections[0].model_copy(
                    update={
                        "findings": [
                            unsafe.sections[0].findings[0].model_copy(
                                update={"metric_ids": ["invented.metric"]}
                            )
                        ]
                    }
                ),
                *unsafe.sections[1:],
            ]
        }
    )
    response = ComprehensiveReportService(
        generator=_Generator(unsafe), model="gpt-test", prompt_version="report-v1"
    ).generate(_catalogue(tmp_path))
    assert response.source == "rule_fallback"

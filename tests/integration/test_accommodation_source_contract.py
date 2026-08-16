from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

import westbusan.orchestrator as orchestrator_module
from westbusan.http import HttpResult, SchemaError
from westbusan.models import SourceSpec
from westbusan.orchestrator import Pipeline
from westbusan.sources.registry import SourceRegistry


def _official_row(record_id: str, jurisdiction: str | None = "6260000") -> dict[str, str]:
    row = {
        "MNG_NO": record_id,
        "BPLC_NM": f"공식숙박-{record_id}",
        "ROAD_NM_ADDR": "부산광역시 사하구 낙동대로 1",
        "LCPMT_YMD": "20200102",
        "SALS_STTS_CD": "01",
        "SALS_STTS_NM": "영업",
        "DTL_SALS_STTS_CD": "01",
        "DTL_SALS_STTS_NM": "정상",
        "LAST_MDFCN_YMD": "20250831",
        "DATA_UPDT_YMD": "20250901",
        "DAT_UPDT_PNT": "01",
        "XCRD": "963210.12",
        "YCRD": "1812345.67",
    }
    if jurisdiction is not None:
        row["OPN_ATMY_GRP_CD"] = jurisdiction
    return row


def _response(rows: list[dict[str, str]], *, total: int, page_no: int = 1) -> bytes:
    return json.dumps(
        {
            "data": rows,
            "totalCount": total,
            "pageNo": page_no,
            "numOfRows": 1,
        },
        ensure_ascii=False,
    ).encode()


def _pipeline(tmp_path: Path) -> tuple[Pipeline, object, object]:
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.fixture_dir = None
    pipeline.settings.service_key = SecretStr("test-service-key")
    pipeline.registry = SourceRegistry(
        (
            SourceSpec(
                "lodgings",
                "https://apis.data.go.kr/1741000/lodgings",
                operation="info",
                group="accommodation",
                required_for_publication=True,
                page_size=1,
                required_parameters={"cond[OPN_ATMY_GRP_CD::EQ]": "6260000"},
                temporal_semantics="current_snapshot_only",
            ),
        )
    )
    pipeline.db.migrate()
    run, _ = pipeline._prepare_run(
        "production", "daily", date(2026, 8, 16), f"source-contract:{tmp_path.name}"
    )
    assert run is not None
    logger = orchestrator_module._JsonlLogger(tmp_path / "logs", date(2026, 8, 16))
    return pipeline, run, logger


def test_collector_pages_with_busan_filter_and_persists_redacted_request_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches page filters or exact credential-free request/response evidence disappearing."""
    pipeline, run, logger = _pipeline(tmp_path)
    requests: list[dict[str, object]] = []

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            requests.append({"endpoint": endpoint, **parameters})
            page_no = int(parameters["pageNo"])
            return HttpResult(
                200,
                _response([_official_row(f"B-{page_no}")], total=2, page_no=page_no),
                "application/json; charset=utf-8",
                retrieved_at=datetime(2026, 8, 16, 1, page_no, tzinfo=UTC),
                response_headers={"etag": f'"page-{page_no}"'},
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    assert pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger) == 2

    assert [request["cond[OPN_ATMY_GRP_CD::EQ]"] for request in requests] == [
        "6260000",
        "6260000",
    ]
    assert pipeline.db.scalar("select count(*) from staging_license_snapshot") == 2
    raw_requests = [
        json.loads(value)
        for (value,) in pipeline.db.query(
            "select request_json from raw_artifact order by request_json"
        )
    ]
    for page_no, metadata in enumerate(raw_requests, start=1):
        assert metadata["endpoint"] == "https://apis.data.go.kr/1741000/lodgings/info"
        assert metadata["parameters"] == {
            "cond[OPN_ATMY_GRP_CD::EQ]": "6260000",
            "serviceKey": "***",
            "pageNo": page_no,
            "numOfRows": 1,
            "returnType": "json",
        }
        assert "as_of" not in metadata["parameters"]
        assert metadata["jurisdiction_filter"] == {
            "parameter": "cond[OPN_ATMY_GRP_CD::EQ]",
            "expected": "6260000",
        }
        assert metadata["response"]["http_status"] == 200
        assert metadata["response"]["content_type"] == "application/json; charset=utf-8"
        assert metadata["response"]["headers"] == {"etag": f'"page-{page_no}"'}
        assert metadata["counts"] == {
            "accepted": 1,
            "out_of_scope": 0,
            "rejected": 0,
        }
    assert pipeline.db.query(
        """select page_no, accepted_count, out_of_scope_count, rejected_count
           from accommodation_collection_audit order by page_no"""
    ) == [(1, 1, 0, 0), (2, 1, 0, 0)]


def test_collector_fails_closed_on_mixed_national_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a provider ignoring the Busan filter and contaminating staging."""
    pipeline, run, logger = _pipeline(tmp_path)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            return HttpResult(
                200,
                _response(
                    [_official_row("BUSAN"), _official_row("SEOUL", "6110000")],
                    total=2,
                ),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="jurisdiction filter"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)

    assert pipeline.db.scalar("select count(*) from raw_artifact") == 1
    assert pipeline.db.scalar("select count(*) from staging_license_snapshot") == 0
    assert pipeline.db.query(
        """select accepted_count, out_of_scope_count, rejected_count
           from accommodation_collection_audit"""
    ) == [(1, 1, 0)]


def test_collector_rejects_nonempty_rows_without_jurisdiction_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an approved field shape silently accepting a missing authority value."""
    pipeline, run, logger = _pipeline(tmp_path)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            return HttpResult(
                200,
                _response([_official_row("MISSING", None)], total=1),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="jurisdiction filter"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)

    assert pipeline.db.query(
        """select accepted_count, out_of_scope_count, rejected_count
           from accommodation_collection_audit"""
    ) == [(0, 0, 1)]


def test_collector_preserves_legitimate_filtered_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a legitimate zero-row Busan response being treated as schema failure."""
    pipeline, run, logger = _pipeline(tmp_path)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            return HttpResult(200, _response([], total=0), "application/json")

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    assert pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger) == 0
    assert pipeline.db.scalar("select count(*) from staging_license_snapshot") == 0
    assert pipeline.db.query(
        """select accepted_count, out_of_scope_count, rejected_count
           from accommodation_collection_audit"""
    ) == [(0, 0, 0)]
    assert pipeline.db.scalar(
        "select status from source_status where source_id = 'lodgings'"
    ) == "EMPTY"

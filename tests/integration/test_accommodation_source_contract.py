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
from westbusan.quality.checks import run_quality_suite
from westbusan.sources.registry import SourceRegistry

BUSAN_DISTRICT_AUTHORITY_CODES = (
    "3250000",
    "3260000",
    "3270000",
    "3280000",
    "3290000",
    "3300000",
    "3310000",
    "3320000",
    "3330000",
    "3340000",
    "3350000",
    "3360000",
    "3370000",
    "3380000",
    "3390000",
    "3400000",
)


def _official_row(record_id: str, jurisdiction: str | None = "3340000") -> dict[str, str]:
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


def _response(
    rows: list[dict[str, str]],
    *,
    total: int,
    page_no: int = 1,
    page_size: int = 1,
) -> bytes:
    return json.dumps(
        {
            "data": rows,
            "totalCount": total,
            "pageNo": page_no,
            "numOfRows": page_size,
        },
        ensure_ascii=False,
    ).encode()


def _pipeline(
    tmp_path: Path,
    *,
    source_id: str = "lodgings",
    page_size: int = 1,
) -> tuple[Pipeline, object, object]:
    pipeline = Pipeline.for_fixtures(tmp_path, Path("tests/fixtures"))
    pipeline.fixture_dir = None
    pipeline.settings.service_key = SecretStr("test-service-key")
    pipeline.registry = SourceRegistry(
        (
            SourceSpec(
                source_id,
                f"https://apis.data.go.kr/1741000/{source_id}",
                operation="info",
                group="accommodation",
                required_for_publication=True,
                page_size=page_size,
                parameter_partitions={
                    "cond[OPN_ATMY_GRP_CD::EQ]": ("3340000",)
                },
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


def _use_current_busan_authority_partitions(
    pipeline: Pipeline, source_id: str = "lodgings"
) -> None:
    pipeline.registry = SourceRegistry(
        (
            SourceSpec(
                source_id,
                f"https://apis.data.go.kr/1741000/{source_id}",
                operation="info",
                group="accommodation",
                required_for_publication=True,
                page_size=100,
                parameter_partitions={
                    "cond[OPN_ATMY_GRP_CD::EQ]": BUSAN_DISTRICT_AUTHORITY_CODES
                },
                temporal_semantics="current_snapshot_only",
            ),
        )
    )


def test_collector_rejects_the_obsolete_city_level_authority_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the provider's obsolete zero-row 6260000 filter being restored."""
    pipeline, run, logger = _pipeline(tmp_path)
    pipeline.registry = SourceRegistry(
        (
            SourceSpec(
                "lodgings",
                "https://apis.data.go.kr/1741000/lodgings",
                operation="info",
                group="accommodation",
                required_for_publication=True,
                required_parameters={
                    "cond[OPN_ATMY_GRP_CD::EQ]": "6260000"
                },
                temporal_semantics="current_snapshot_only",
            ),
        )
    )

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            return HttpResult(200, _response([], total=0), "application/json")

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="reviewed Busan jurisdiction filter"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)


def test_collector_reconciles_all_current_busan_authority_partitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a city-level code collecting zero rows or shard totals being reconciled as one page set."""
    pipeline, run, logger = _pipeline(tmp_path)
    _use_current_busan_authority_partitions(pipeline)
    requests: list[str] = []

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            authority = str(parameters["cond[OPN_ATMY_GRP_CD::EQ]"])
            requests.append(authority)
            rows = (
                [_official_row(f"ROW-{authority}", authority)]
                if authority in {"3340000", "3360000"}
                else []
            )
            return HttpResult(
                200,
                _response(rows, total=len(rows), page_size=100),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    loaded = pipeline._collect_accommodation(
        run, "lodgings", date(2026, 8, 16), logger
    )

    assert loaded == 2
    assert requests == list(BUSAN_DISTRICT_AUTHORITY_CODES)
    assert pipeline.db.query(
        """select jurisdiction_code from staging_license_snapshot
           order by jurisdiction_code"""
    ) == [("3340000",), ("3360000",)]
    assert pipeline.db.scalar(
        "select count(*) from accommodation_collection_audit"
    ) == 16
    reconciliation = next(
        check
        for check in run_quality_suite(pipeline.db, run.run_id).checks
        if check.name == "raw_total_matches_staging" and check.source_id == "lodgings"
    )
    assert reconciliation.status == "passed"
    assert reconciliation.actual[0]["raw_total"] == 2
    assert reconciliation.actual[0]["target_rows"] == 2


def test_quality_rejects_missing_empty_authority_partition_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches deleted zero-row shard evidence being hidden by an unchanged aggregate total."""
    pipeline, run, logger = _pipeline(tmp_path)
    _use_current_busan_authority_partitions(pipeline)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            authority = str(parameters["cond[OPN_ATMY_GRP_CD::EQ]"])
            rows = (
                [_official_row(f"ROW-{authority}", authority)]
                if authority in {"3340000", "3360000"}
                else []
            )
            return HttpResult(
                200,
                _response(rows, total=len(rows), page_size=100),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)
    pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)
    pipeline.db.connection.execute(
        """delete from raw_artifact
           where run_id = ?
             and request_json not like '%3340000%'
             and request_json not like '%3360000%'""",
        [run.run_id],
    )

    reconciliation = next(
        check
        for check in run_quality_suite(pipeline.db, run.run_id).checks
        if check.name == "raw_total_matches_staging" and check.source_id == "lodgings"
    )

    assert reconciliation.status == "failed"


def test_collector_rejects_a_row_from_another_authority_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a provider ignoring one shard filter while returning another Busan district."""
    pipeline, run, logger = _pipeline(tmp_path)
    _use_current_busan_authority_partitions(pipeline)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            requested = str(parameters["cond[OPN_ATMY_GRP_CD::EQ]"])
            rows = (
                [_official_row("WRONG-SHARD", "3360000")]
                if requested == "3340000"
                else []
            )
            return HttpResult(
                200,
                _response(rows, total=len(rows), page_size=100),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="jurisdiction filter"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)

    assert pipeline.db.query(
        """select jurisdiction_expected, accepted_count, out_of_scope_count
           from accommodation_collection_audit
           where out_of_scope_count > 0"""
    ) == [("3340000", 0, 1)]
    assert pipeline.db.scalar("select count(*) from staging_license_snapshot") == 0


@pytest.mark.parametrize(
    "source_id",
    [
        "lodgings",
        "tourist_accommodations",
        "foreigner_city_homestays",
        "rural_homestays",
        "hanok_experience",
        "tourist_pensions",
    ],
)
def test_every_accommodation_source_follows_provider_page_caps_until_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
) -> None:
    """Catches requested page size being used to stop before a provider-capped page two."""
    pipeline, run, logger = _pipeline(
        tmp_path, source_id=source_id, page_size=1000
    )
    requested_pages: list[int] = []

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            page_no = int(parameters["pageNo"])
            requested_pages.append(page_no)
            rows = (
                [_official_row(f"{source_id}-1"), _official_row(f"{source_id}-2")]
                if page_no == 1
                else [_official_row(f"{source_id}-3")]
            )
            return HttpResult(
                200,
                _response(rows, total=3, page_no=page_no, page_size=2),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    loaded = pipeline._collect_accommodation(
        run, source_id, date(2026, 8, 16), logger
    )

    assert loaded == 3
    assert requested_pages == [1, 2]
    assert pipeline.db.scalar(
        "select count(*) from staging_license_snapshot where source_id = ?",
        [source_id],
    ) == 3
    checkpoint = json.loads(
        pipeline.db.scalar(
            """select checkpoint_json from collection_checkpoint
               where source_id = ?""",
            [source_id],
        )
    )
    evidence = checkpoint["evidence"]
    assert evidence["received_rows"] == 3
    assert evidence["total_count"] == 3
    assert evidence["counts"] == {"accepted": 3, "out_of_scope": 0, "rejected": 0}
    assert evidence["jurisdiction_filter"] == {
        "expected": "3340000",
        "parameter": "cond[OPN_ATMY_GRP_CD::EQ]",
    }
    assert evidence["operation"] == "info"


@pytest.mark.parametrize("missing_key", ["totalCount", "pageNo", "numOfRows"])
def test_nonempty_accommodation_response_requires_actual_paging_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
) -> None:
    """Catches invented paging defaults making an unreviewed response publishable."""
    pipeline, run, logger = _pipeline(tmp_path)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            envelope = {
                "data": [_official_row("MISSING-METADATA")],
                "totalCount": 1,
                "pageNo": 1,
                "numOfRows": 1,
            }
            del envelope[missing_key]
            return HttpResult(200, json.dumps(envelope).encode(), "application/json")

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="paging metadata"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)

    assert pipeline.db.scalar("select count(*) from staging_license_snapshot") == 0


def test_accommodation_response_page_must_match_nonfirst_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches page one being replayed when the collector requested page two."""
    pipeline, run, logger = _pipeline(tmp_path)
    partition = "snapshot:2026-08-16"
    pipeline._checkpoint(
        "lodgings",
        partition,
        "running",
        2,
        run.run_id,
        evidence={"received_rows": 1, "total_count": 2},
    )

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            assert parameters["pageNo"] == 2
            return HttpResult(
                200,
                _response([_official_row("REPLAY")], total=2, page_no=1),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="requested page"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)

    assert pipeline.db.scalar("select count(*) from staging_license_snapshot") == 0


def test_accommodation_total_must_remain_stable_after_page_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a changing total turning page reconciliation into guesswork."""
    pipeline, run, logger = _pipeline(tmp_path)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            page_no = int(parameters["pageNo"])
            return HttpResult(
                200,
                _response(
                    [_official_row(f"PAGE-{page_no}")],
                    total=2 if page_no == 1 else 3,
                    page_no=page_no,
                ),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="totalCount changed"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)


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
        "3340000",
        "3340000",
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
            "cond[OPN_ATMY_GRP_CD::EQ]": "3340000",
            "serviceKey": "***",
            "pageNo": page_no,
            "numOfRows": 1,
            "returnType": "json",
        }
        assert "as_of" not in metadata["parameters"]
        assert metadata["jurisdiction_filter"] == {
            "parameter": "cond[OPN_ATMY_GRP_CD::EQ]",
            "expected": "3340000",
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
    reconciliation = next(
        check
        for check in run_quality_suite(pipeline.db, run.run_id).checks
        if check.name == "raw_total_matches_staging" and check.source_id == "lodgings"
    )
    assert reconciliation.status == "passed"
    assert reconciliation.actual[0]["raw_total"] == 2
    assert reconciliation.actual[0]["target_rows"] == 2


def test_collector_fails_closed_on_mixed_national_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a provider ignoring the Busan filter and contaminating staging."""
    pipeline, run, logger = _pipeline(tmp_path, page_size=2)

    class Client:
        def get(self, endpoint: str, parameters: dict[str, object]) -> HttpResult:
            return HttpResult(
                200,
                _response(
                    [_official_row("BUSAN"), _official_row("SEOUL", "6110000")],
                    total=2,
                    page_size=2,
                ),
                "application/json",
            )

    monkeypatch.setattr(orchestrator_module, "SafeHttpClient", Client)

    with pytest.raises(SchemaError, match="jurisdiction filter"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)
    with pytest.raises(SchemaError, match="jurisdiction filter"):
        pipeline._collect_accommodation(run, "lodgings", date(2026, 8, 16), logger)

    assert pipeline.db.scalar("select count(*) from raw_artifact") == 2
    assert pipeline.db.scalar("select count(*) from staging_license_snapshot") == 0
    assert pipeline.db.query(
        """select accepted_count, out_of_scope_count, rejected_count
           from accommodation_collection_audit"""
    ) == [(1, 1, 0), (1, 1, 0)]


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

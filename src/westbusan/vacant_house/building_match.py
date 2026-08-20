"""Deterministic matching of vacant-house records to pinned building evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from westbusan.db import Database
from westbusan.vacant_house.assessment_models import AssessmentInputs, BuildingMatch


class _CodedRecord(Protocol):
    district_code: str | None
    legal_dong_code: str | None
    lot_type: str | None
    main_lot: str | None
    sub_lot: str | None
    road_code: str | None
    building_main: str | None
    building_sub: str | None
    dong_name: str | None
    unit_name: str | None


@dataclass(frozen=True, slots=True)
class _BuildingCandidate:
    building_id: str
    observed_on: date
    district_code: str | None
    legal_dong_code: str | None
    lot_type: str | None
    main_lot: str | None
    sub_lot: str | None
    road_code: str | None
    building_main: str | None
    building_sub: str | None


def match_building(
    db: Database,
    inputs: AssessmentInputs,
    record: _CodedRecord | Mapping[str, object],
) -> BuildingMatch:
    """Match one record using only coded identities visible in its pinned core run.

    The building-register revision scope is the core run's recorded input lineage.
    Address labels are deliberately not loaded or compared.
    """
    core_period, rows = _pinned_building_rows(db, inputs)
    pinned_candidates = tuple(_candidate_from_row(row) for row in rows)
    candidates, tied_candidates = _separate_tied_revisions(pinned_candidates)
    tied_matches = _unique_candidates(
        _parcel_matches(tied_candidates, record), _road_matches(tied_candidates, record)
    )
    if tied_matches:
        return _unresolved_match(
            inputs,
            record,
            core_period,
            "ambiguous_pinned_revisions",
            tied_matches,
            "pinned_revision",
        )
    parcel_matches = _parcel_matches(candidates, record)
    road_matches = _road_matches(candidates, record)

    if len(parcel_matches) > 1:
        if len(road_matches) == 1:
            road = road_matches[0]
            if road.building_id in {candidate.building_id for candidate in parcel_matches}:
                return _resolved_match(
                    inputs, record, road, "exact_road_building_single", "road"
                )
            return _unresolved_match(
                inputs,
                record,
                core_period,
                "conflicting_exact_identities",
                _unique_candidates(parcel_matches, road_matches),
                "parcel_and_road",
            )
        return _unresolved_match(
            inputs, record, core_period, "ambiguous_multiple_buildings", parcel_matches,
            "parcel",
        )
    if len(parcel_matches) == 1:
        parcel = parcel_matches[0]
        if len(road_matches) > 1:
            return _unresolved_match(
                inputs,
                record,
                core_period,
                "ambiguous_multiple_buildings",
                (parcel, *road_matches),
                "parcel_and_road",
            )
        if len(road_matches) == 1 and road_matches[0].building_id != parcel.building_id:
            return _unresolved_match(
                inputs,
                record,
                core_period,
                "conflicting_exact_identities",
                (parcel, road_matches[0]),
                "parcel_and_road",
            )
        return _resolved_match(inputs, record, parcel, "exact_parcel_single", "parcel")

    if len(road_matches) == 1:
        return _resolved_match(
            inputs, record, road_matches[0], "exact_road_building_single", "road"
        )
    if len(road_matches) > 1:
        return _unresolved_match(
            inputs, record, core_period, "ambiguous_multiple_buildings", road_matches,
            "road",
        )
    return _unresolved_match(inputs, record, core_period, "no_match", (), "none")


def _pinned_building_rows(
    db: Database, inputs: AssessmentInputs
) -> tuple[date, list[tuple[object, ...]]]:
    """Read unambiguously latest revisions from the current core publication."""
    rows = db.query(
        """with core as (
                 select run.run_id, run.business_date
                 from pipeline_run as run
                 join publication_state as publication
                   on publication.publication_key = 'current'
                  and publication.published_run_id = run.run_id
                 where run.run_id = ?
             ), visible_revisions as (
                 select revision.building_id, revision.observed_on,
                        revision.sigungu_cd, revision.bjdong_cd,
                        revision.plat_gb_cd, revision.bun, revision.ji,
                        revision.source_payload_json,
                        rank() over (
                            partition by revision.building_id
                            order by producer.business_date desc,
                                     producer.started_at desc,
                                     revision.observed_on desc,
                                     revision.revision_sequence desc
                        ) as revision_rank
                 from staging_building_revision as revision
                 join pipeline_run_input as lineage
                   on lineage.input_run_id = revision.version_run_id
                 join core on core.run_id = lineage.run_id
                 join pipeline_run as producer
                   on producer.run_id = revision.version_run_id
                 where revision.observed_on <= core.business_date
                   and producer.business_date <= core.business_date
             )
             select core.business_date, building_id, observed_on,
                    sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji,
                    source_payload_json
             from core
             left join visible_revisions
               on revision_rank = 1
             order by building_id""",
        [inputs.base_published_run_id],
    )
    if not rows:
        raise ValueError("pinned_core_run_not_published")
    core_period = rows[0][0]
    if not isinstance(core_period, date):
        raise TypeError("pinned_core_run_missing_business_date")
    return core_period, [row[1:] for row in rows if row[1] is not None]


def _separate_tied_revisions(
    candidates: tuple[_BuildingCandidate, ...],
) -> tuple[tuple[_BuildingCandidate, ...], tuple[_BuildingCandidate, ...]]:
    by_building: dict[str, list[_BuildingCandidate]] = {}
    for candidate in candidates:
        by_building.setdefault(candidate.building_id, []).append(candidate)
    resolved = tuple(
        revisions[0]
        for _, revisions in sorted(by_building.items())
        if len(revisions) == 1
    )
    tied = tuple(
        candidate
        for _, revisions in sorted(by_building.items())
        if len(revisions) > 1
        for candidate in revisions
    )
    return resolved, tied


def _unique_candidates(
    *groups: tuple[_BuildingCandidate, ...],
) -> tuple[_BuildingCandidate, ...]:
    by_building: dict[str, _BuildingCandidate] = {}
    for candidate in (candidate for group in groups for candidate in group):
        by_building.setdefault(candidate.building_id, candidate)
    return tuple(by_building[building_id] for building_id in sorted(by_building))


def _candidate_from_row(row: tuple[object, ...]) -> _BuildingCandidate:
    payload = _payload(row[7])
    return _BuildingCandidate(
        building_id=str(row[0]),
        observed_on=_date_value(row[1]),
        district_code=_code(row[2], 5),
        legal_dong_code=_code(row[3], 5),
        lot_type=_code(row[4], 1),
        main_lot=_code(row[5], 4),
        sub_lot=_code(row[6], 4),
        road_code=_payload_code(payload, ("rnmgtSn", "roadCode"), 12),
        building_main=_payload_code(payload, ("buldMnnm", "buildingMain"), 5),
        building_sub=_payload_code(payload, ("buldSlno", "buildingSub"), 5),
    )


def _parcel_matches(
    candidates: tuple[_BuildingCandidate, ...], record: _CodedRecord | Mapping[str, object]
) -> tuple[_BuildingCandidate, ...]:
    identity = (
        _record_code(record, "district_code", 5),
        _record_code(record, "legal_dong_code", 5),
        _record_code(record, "lot_type", 1),
        _record_code(record, "main_lot", 4),
        _record_code(record, "sub_lot", 4),
    )
    if any(value is None for value in identity):
        return ()
    return tuple(
        candidate
        for candidate in candidates
        if identity
        == (
            candidate.district_code,
            candidate.legal_dong_code,
            candidate.lot_type,
            candidate.main_lot,
            candidate.sub_lot,
        )
    )


def _road_matches(
    candidates: tuple[_BuildingCandidate, ...], record: _CodedRecord | Mapping[str, object]
) -> tuple[_BuildingCandidate, ...]:
    identity = (
        _record_code(record, "road_code", 12),
        _record_code(record, "building_main", 5),
        _record_code(record, "building_sub", 5),
    )
    if any(value is None for value in identity):
        return ()
    return tuple(
        candidate
        for candidate in candidates
        if identity
        == (candidate.road_code, candidate.building_main, candidate.building_sub)
    )


def _resolved_match(
    inputs: AssessmentInputs,
    record: _CodedRecord | Mapping[str, object],
    candidate: _BuildingCandidate,
    quality: str,
    basis: str,
) -> BuildingMatch:
    return BuildingMatch(
        building_id=candidate.building_id,
        quality=quality,
        source_period=candidate.observed_on,
        evidence=_safe_evidence(inputs, record, basis, ()),
    )


def _unresolved_match(
    inputs: AssessmentInputs,
    record: _CodedRecord | Mapping[str, object],
    source_period: date,
    quality: str,
    candidates: tuple[_BuildingCandidate, ...],
    basis: str,
) -> BuildingMatch:
    return BuildingMatch(
        building_id=None,
        quality=quality,
        source_period=source_period,
        evidence=_safe_evidence(inputs, record, basis, candidates),
    )


def _safe_evidence(
    inputs: AssessmentInputs,
    record: _CodedRecord | Mapping[str, object],
    basis: str,
    candidates: tuple[_BuildingCandidate, ...],
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "match_basis": basis,
        "pinned_core_run_sha256": _hash(str(inputs.base_published_run_id)),
        "candidate_count": len({candidate.building_id for candidate in candidates}),
    }
    for field_name in ("dong_name", "unit_name"):
        value = _record_value(record, field_name)
        if value is not None:
            evidence[f"record_{field_name.removesuffix('_name')}_sha256"] = _hash(
                str(value)
            )
    if candidates:
        evidence["candidate_id_sha256"] = tuple(
            sorted({_hash(candidate.building_id) for candidate in candidates})
        )
    return evidence


def _payload(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _payload_code(payload: Mapping[str, object], aliases: tuple[str, ...], width: int) -> str | None:
    normalized = {
        "".join(character for character in str(key).casefold() if character.isalnum()): value
        for key, value in payload.items()
    }
    for alias in aliases:
        value = normalized.get("".join(character for character in alias.casefold() if character.isalnum()))
        result = _code(value, width)
        if result is not None:
            return result
    return None


def _record_code(record: _CodedRecord | Mapping[str, object], field_name: str, width: int) -> str | None:
    return _code(_record_value(record, field_name), width)


def _record_value(record: _CodedRecord | Mapping[str, object], field_name: str) -> object | None:
    if isinstance(record, Mapping):
        return record.get(field_name)
    return getattr(record, field_name, None)


def _code(value: object, width: int) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text.isdigit() or len(text) > width:
        return None
    return text.zfill(width)


def _date_value(value: object) -> date:
    if not isinstance(value, date):
        raise TypeError("pinned_building_missing_observation_date")
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["match_building"]

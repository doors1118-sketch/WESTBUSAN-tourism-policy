"""Conservative, deterministic resolution of legal registrations into facilities."""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from rapidfuzz.fuzz import ratio

from westbusan.accommodation.normalize import LicenseRecord
from westbusan.db import Database, ensure_run_rebuildable
from westbusan.entity_resolution.normalize import (
    normalize_address,
    normalize_name,
    normalize_phone,
)
from westbusan.inventory import is_active_status, latest_complete_snapshot_runs
from westbusan.revisions import (
    latest_immutable_license_records,
    require_immutable_for_overwritten_completed_runs,
)

DecisionLabel = Literal["auto_merge", "designation_link", "review", "separate"]
ALGORITHM_VERSION = "entity-resolution-v2"


@dataclass(frozen=True, slots=True)
class MatchFeatures:
    """Evidence calculated without making a merge decision."""

    source_management_match: bool
    official_record_identity: bool
    building_match: bool
    address_match: bool
    address_unit_match: bool
    coordinate_distance_metres: float | None
    name_similarity: float
    phones_match: bool
    one_phone_missing: bool
    phones_conflict: bool
    is_candidate: bool


@dataclass(frozen=True, slots=True)
class MatchDecision:
    """The conservative decision and its durable, reviewable evidence."""

    label: DecisionLabel
    features: MatchFeatures


@dataclass(frozen=True, slots=True)
class FacilityBuildResult:
    """Counts emitted by one deterministic facility rebuild."""

    facility_count: int
    license_links: int
    review_pairs: int
    designation_links: int
    unmatched_designations: int


@dataclass(frozen=True, slots=True)
class AutoMergeCalibration:
    """Versioned representative-sample result with sampling uncertainty."""

    sample_version: str
    algorithm_version: str
    predicted_positive: int
    true_positive: int
    point_precision: float
    confidence_lower_bound: float


def candidate_features(left: LicenseRecord, right: LicenseRecord) -> MatchFeatures:
    """Calculate candidate evidence for two normalized license observations."""
    return _features(_license_mapping(left), _license_mapping(right))


def classify_pair(left: Mapping[str, object], right: Mapping[str, object]) -> MatchDecision:
    """Classify one possible pair without treating shared address as a merge."""
    features = _features(left, right)
    if (_is_tourist_pension(left) or _is_tourist_pension(right)) and (
        features.source_management_match
        or (features.building_match and features.name_similarity >= 0.90)
    ):
        return MatchDecision("designation_link", features)
    if features.official_record_identity:
        return MatchDecision("auto_merge", features)
    if (
        features.building_match
        and features.phones_match
        and features.name_similarity >= 0.90
    ):
        return MatchDecision("auto_merge", features)
    if (
        features.address_match
        and features.name_similarity >= 0.94
        and (features.phones_match or features.address_unit_match)
    ):
        return MatchDecision("auto_merge", features)
    if features.address_match and not _has_name_or_phone(left, right):
        return MatchDecision("review", features)
    if features.phones_conflict and features.name_similarity < 0.75:
        return MatchDecision("separate", features)
    return MatchDecision("review" if features.is_candidate else "separate", features)


def evaluate_auto_merge_precision(labeled_pairs: Path, matcher: Callable) -> float:
    """Return precision of positive automatic merges in a labeled CSV sample."""
    predicted_positive = 0
    true_positive = 0
    with Path(labeled_pairs).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            left, right = _labeled_pair(row)
            outcome = matcher(left, right)
            label = outcome.label if isinstance(outcome, MatchDecision) else str(outcome)
            if label != "auto_merge":
                continue
            predicted_positive += 1
            if row["expected"] == "auto_merge":
                true_positive += 1
    if predicted_positive < 10:
        raise ValueError(
            "labeled entity-resolution sample must produce at least 10 auto_merge predictions"
        )
    return true_positive / predicted_positive if predicted_positive else 0.0


def evaluate_auto_merge_calibration(
    labeled_pairs: Path,
    matcher: Callable,
    *,
    sample_version: str,
) -> AutoMergeCalibration:
    """Evaluate a reviewed sample and retain a conservative Wilson lower bound."""
    predicted_positive = 0
    true_positive = 0
    with Path(labeled_pairs).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            left, right = _labeled_pair(row)
            outcome = matcher(left, right)
            label = outcome.label if isinstance(outcome, MatchDecision) else str(outcome)
            if label == "auto_merge":
                predicted_positive += 1
                true_positive += row["expected"] == "auto_merge"
    if predicted_positive < 10:
        raise ValueError(
            "representative calibration must produce at least 10 auto_merge predictions"
        )
    precision = true_positive / predicted_positive
    z = 1.959963984540054
    denominator = 1 + z * z / predicted_positive
    centre = precision + z * z / (2 * predicted_positive)
    margin = z * math.sqrt(
        precision * (1 - precision) / predicted_positive
        + z * z / (4 * predicted_positive * predicted_positive)
    )
    return AutoMergeCalibration(
        sample_version=sample_version,
        algorithm_version=ALGORITHM_VERSION,
        predicted_positive=predicted_positive,
        true_positive=true_positive,
        point_precision=precision,
        confidence_lower_bound=(centre - margin) / denominator,
    )


def record_pair_adjudication(
    db: Database,
    left_registration_key: str,
    right_registration_key: str,
    *,
    decision: Literal["merge", "separate"],
    reviewer: str,
    rationale: str,
    data_version: str,
) -> None:
    """Append an immutable, versioned human adjudication for one stable pair."""
    left, right = sorted((left_registration_key, right_registration_key))
    db.connection.execute(
        """
        insert into entity_pair_adjudication (
            left_registration_key, right_registration_key, decision, reviewer,
            rationale, algorithm_version, data_version
        ) values (?, ?, ?, ?, ?, ?, ?)
        """,
        [left, right, decision, reviewer, rationale, ALGORITHM_VERSION, data_version],
    )


def build_facilities(
    db: Database,
    run_id: UUID,
    *,
    progress: Callable[[], None] | None = None,
    fence_check: Callable[[], None] | None = None,
) -> FacilityBuildResult:
    """Build physical facilities from latest snapshots, preserving every registration."""
    heartbeat = progress or (lambda: None)
    guard = fence_check or (lambda: None)

    def write(sql: str, parameters: list[object] | None = None) -> None:
        if fence_check is None:
            db.connection.execute(sql, parameters or [])
            return
        began = False
        try:
            db.connection.execute("begin transaction")
            began = True
            guard()
            db.connection.execute(sql, parameters or [])
            guard()
            db.connection.execute("commit")
            began = False
        except Exception:
            if began:
                db.connection.execute("rollback")
            raise

    heartbeat()
    records = _latest_records(db, run_id)
    building_ids = _building_ids(db, run_id)
    adjudications = _adjudications(db)
    calibrated = _default_calibration_allows_auto_merge()
    for record in records:
        record["building_ids"] = building_ids.get(_registration_key(record), set())

    decisions: list[tuple[dict[str, object], dict[str, object], MatchDecision]] = []
    for left, right in combinations(records, 2):
        heartbeat()
        decision = classify_pair(left, right)
        pair = tuple(sorted((_registration_key(left), _registration_key(right))))
        adjudicated = adjudications.get(pair)
        if adjudicated == "merge":
            decision = MatchDecision("auto_merge", decision.features)
        elif adjudicated == "separate":
            decision = MatchDecision("separate", decision.features)
        elif decision.label == "auto_merge" and not calibrated:
            decision = MatchDecision("review", decision.features)
        if not decision.features.is_candidate and adjudicated is None:
            continue
        decisions.append((left, right, decision))

    physical_records = [record for record in records if not _is_tourist_pension(record)]
    physical_keys = {_registration_key(record) for record in physical_records}
    physical_decisions = [
        decision
        for decision in decisions
        if _registration_key(decision[0]) in physical_keys
        and _registration_key(decision[1]) in physical_keys
    ]
    merge_edges, blocked_edges = _safe_physical_merge_edges(
        physical_records, physical_decisions
    )
    physical_components = _components(physical_records, merge_edges)
    components, designation_targets, unmatched_designations = _attach_designations(
        records, decisions, physical_components
    )
    component_facility_ids = _canonical_component_ids(
        db,
        {tuple(keys) for keys in components.values()},
        run_id,
        write,
    )

    evidence_by_key: dict[str, list[dict[str, object]]] = defaultdict(list)
    review_rows: dict[UUID, tuple[UUID | None, UUID | None, str]] = {}

    def add_review(
        review_key: str,
        left_id: UUID | None,
        right_id: UUID | None,
        evidence: dict[str, object],
    ) -> None:
        if left_id is not None and left_id == right_id:
            return
        review_rows[uuid5(NAMESPACE_URL, review_key)] = (
            left_id,
            right_id,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        )

    accepted_edges = {tuple(sorted(edge)) for edge in merge_edges}
    for left, right, decision in decisions:
        left_key = _registration_key(left)
        right_key = _registration_key(right)
        evidence = {
            "decision": decision.label,
            "left_registration_key": left_key,
            "right_registration_key": right_key,
            "features": asdict(decision.features),
        }
        if (
            decision.label == "auto_merge"
            and tuple(sorted((left_key, right_key))) in accepted_edges
        ) or (
            decision.label == "designation_link"
            and left_key in components
            and right_key in components
            and components[left_key] == components[right_key]
        ):
            evidence_by_key[left_key].append(evidence)
            evidence_by_key[right_key].append(evidence)
        elif decision.label == "review" and left_key in components and right_key in components:
            add_review(
                "duplicate-review:" + "|".join(sorted((left_key, right_key))),
                component_facility_ids[components[left_key]],
                component_facility_ids[components[right_key]],
                evidence,
            )

    for left, right, witnesses in blocked_edges:
        left_key = _registration_key(left)
        right_key = _registration_key(right)
        add_review(
            "blocked-transitive-merge:" + "|".join(sorted((left_key, right_key))),
            component_facility_ids[components[left_key]],
            component_facility_ids[components[right_key]],
            {
                "decision": "blocked_transitive_merge",
                "left_registration_key": left_key,
                "right_registration_key": right_key,
                "witnesses": witnesses,
            },
        )

    for key, reason in unmatched_designations.items():
        add_review(
            "unmatched-designation:" + key,
            None,
            None,
            {
                "decision": "unmatched_designation",
                "registration_key": key,
                "reason": reason,
            },
        )

    records_by_key = {_registration_key(record): record for record in physical_records}
    desired_facilities: set[UUID] = set()
    desired_license_links: set[tuple[UUID, str, str]] = set()
    desired_building_links: set[tuple[UUID, UUID]] = set()
    heartbeat()
    guard()
    write("delete from run_duplicate_review where run_id = ?", [run_id])
    write("delete from run_facility_building where run_id = ?", [run_id])
    write("delete from run_facility_license where run_id = ?", [run_id])
    write("delete from run_facility where run_id = ?", [run_id])
    write(
        "delete from facility_component_history where run_id = ?", [run_id]
    )
    write(
        "delete from facility_designation_history where run_id = ?", [run_id]
    )
    for component in {tuple(keys) for keys in components.values()}:
        guard()
        facility_id = component_facility_ids[component]
        desired_facilities.add(facility_id)
        component_records = [records_by_key[key] for key in component]
        canonical = next(
            (record["source_name"] for record in component_records if record["source_name"]),
            None,
        )
        district = next((record["district"] for record in component_records if record["district"]), None)
        region_group = next(
            (record["region_group"] for record in component_records if record["region_group"]),
            None,
        )
        write(
            """
            insert into dim_facility (facility_id, canonical_name, district, region_group)
            values (?, ?, ?, ?)
            on conflict (facility_id) do update set
                canonical_name = excluded.canonical_name,
                district = excluded.district,
                region_group = excluded.region_group
            """,
            [facility_id, canonical, district, region_group],
        )
        write(
            """insert into run_facility (
                   run_id, facility_id, canonical_name, district, region_group
               ) values (?, ?, ?, ?, ?)""",
            [run_id, facility_id, canonical, district, region_group],
        )
        for record in component_records:
            guard()
            key = _registration_key(record)
            source_id = str(record["source_id"])
            source_record_id = str(record["source_record_id"])
            desired_license_links.add((facility_id, source_id, source_record_id))
            write(
                """
                insert into facility_component_history (
                    run_id, facility_id, source_id, source_record_id,
                    source_snapshot_run_id, component_signature, district,
                    region_group
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (run_id, source_id, source_record_id) do update set
                    facility_id = excluded.facility_id,
                    source_snapshot_run_id = excluded.source_snapshot_run_id,
                    component_signature = excluded.component_signature,
                    district = excluded.district,
                    region_group = excluded.region_group
                """,
                [
                    run_id,
                    facility_id,
                    source_id,
                    source_record_id,
                    record["selected_version_run_id"],
                    "|".join(component),
                    district,
                    region_group,
                ],
            )
            evidence = json.dumps(
                {"registration_key": key, "merge_evidence": evidence_by_key[key]},
                ensure_ascii=False,
                sort_keys=True,
            )
            write(
                """
                insert into bridge_facility_license
                    (facility_id, source_id, source_record_id, evidence_json)
                values (?, ?, ?, ?)
                on conflict (facility_id, source_id, source_record_id) do update set
                    evidence_json = excluded.evidence_json
                """,
                [facility_id, source_id, source_record_id, evidence],
            )
            write(
                """insert into run_facility_license (
                       run_id, facility_id, source_id, source_record_id, evidence_json,
                       selected_version_run_id, selected_observed_on,
                       selected_revision_sequence
                   ) values (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    run_id,
                    facility_id,
                    source_id,
                    source_record_id,
                    evidence,
                    record["selected_version_run_id"],
                    record["selected_observed_on"],
                    record["selected_revision_sequence"],
                ],
            )
            for building_id in record["building_ids"]:
                desired_building_links.add((facility_id, building_id))
                write(
                    """
                    insert into bridge_facility_building (facility_id, building_id)
                    values (?, ?)
                    on conflict do nothing
                    """,
                    [facility_id, building_id],
                )
                write(
                    """insert into run_facility_building (
                           run_id, facility_id, building_id
                       ) values (?, ?, ?)""",
                    [run_id, facility_id, building_id],
                )

    desired_designation_links: set[tuple[UUID, str, str]] = set()
    for designation_key, component in designation_targets.items():
        designation = next(
            record for record in records if _registration_key(record) == designation_key
        )
        facility_id = component_facility_ids[component]
        source_id = str(designation["source_id"])
        source_record_id = str(designation["source_record_id"])
        desired_designation_links.add((facility_id, source_id, source_record_id))
        guard()
        write(
            """
            insert into facility_designation_history (
                run_id, facility_id, source_id, source_record_id,
                source_snapshot_run_id
            ) values (?, ?, ?, ?, ?)
            on conflict (run_id, source_id, source_record_id) do update set
                facility_id = excluded.facility_id,
                source_snapshot_run_id = excluded.source_snapshot_run_id
            """,
            [
                run_id,
                facility_id,
                source_id,
                source_record_id,
                designation["selected_version_run_id"],
            ],
        )
        write(
            """
            insert into bridge_facility_designation (
                facility_id, source_id, source_record_id, evidence_json
            ) values (?, ?, ?, ?)
            on conflict (facility_id, source_id, source_record_id) do update set
                evidence_json = excluded.evidence_json
            """,
            [
                facility_id,
                source_id,
                source_record_id,
                json.dumps(
                    {
                        "decision": "designation_link",
                        "registration_key": designation_key,
                        "physical_component": component,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ],
        )

    for facility_id, source_id, source_record_id in db.query(
        "select facility_id, source_id, source_record_id from bridge_facility_license"
    ):
        if (facility_id, str(source_id), str(source_record_id)) not in desired_license_links:
            guard()
            write(
                """
                delete from bridge_facility_license
                where facility_id = ? and source_id = ? and source_record_id = ?
                """,
                [facility_id, source_id, source_record_id],
            )
    for facility_id, building_id in db.query(
        "select facility_id, building_id from bridge_facility_building"
    ):
        if (facility_id, building_id) not in desired_building_links:
            guard()
            write(
                "delete from bridge_facility_building where facility_id = ? and building_id = ?",
                [facility_id, building_id],
            )
    for facility_id, source_id, source_record_id in db.query(
        "select facility_id, source_id, source_record_id from bridge_facility_designation"
    ):
        if (facility_id, str(source_id), str(source_record_id)) not in desired_designation_links:
            guard()
            write(
                """
                delete from bridge_facility_designation
                where facility_id = ? and source_id = ? and source_record_id = ?
                """,
                [facility_id, source_id, source_record_id],
            )
    for (facility_id,) in db.query("select facility_id from dim_facility"):
        if facility_id not in desired_facilities:
            guard()
            write("delete from dim_facility where facility_id = ?", [facility_id])

    guard()
    write("delete from duplicate_review where review_status = 'pending'")
    for review_id, (left_id, right_id, evidence) in review_rows.items():
        guard()
        write(
            """
            insert into duplicate_review
                (review_id, left_facility_id, right_facility_id, evidence_json)
            values (?, ?, ?, ?)
            on conflict (review_id) do update set
                left_facility_id = excluded.left_facility_id,
                right_facility_id = excluded.right_facility_id,
                evidence_json = excluded.evidence_json
            """,
            [review_id, left_id, right_id, evidence],
        )
        write(
            """insert into run_duplicate_review (
                   run_id, review_id, left_facility_id, right_facility_id,
                   review_status, evidence_json
               ) select ?, review_id, left_facility_id, right_facility_id,
                        review_status, evidence_json
                 from duplicate_review where review_id = ?""",
            [run_id, review_id],
        )

    heartbeat()
    guard()
    return FacilityBuildResult(
        facility_count=len({tuple(keys) for keys in components.values()}),
        license_links=len(physical_components),
        review_pairs=len(review_rows),
        designation_links=sum(_is_tourist_pension(record) for record in records)
        - len(unmatched_designations),
        unmatched_designations=len(unmatched_designations),
    )


def _features(left: Mapping[str, object], right: Mapping[str, object]) -> MatchFeatures:
    left_record = _source_record_id(left)
    right_record = _source_record_id(right)
    source_management_match = bool(left_record and right_record and left_record == right_record)
    official_record_identity = source_management_match and _source_id(left) == _source_id(right)
    left_buildings = _building_values(left)
    right_buildings = _building_values(right)
    building_match = bool(left_buildings & right_buildings)
    address_match = bool(_address_values(left) & _address_values(right))
    address_unit_match = bool(_address_unit_values(left) & _address_unit_values(right))
    left_name = _normalized_name(left)
    right_name = _normalized_name(right)
    name_similarity = (
        ratio(left_name, right_name) / 100 if left_name is not None and right_name is not None else 0.0
    )
    left_phone = _phone(left)
    right_phone = _phone(right)
    phones_match = bool(left_phone and right_phone and left_phone == right_phone)
    one_phone_missing = (left_phone is None) != (right_phone is None)
    phones_conflict = bool(left_phone and right_phone and left_phone != right_phone)
    distance = _coordinate_distance(left, right)
    is_candidate = (
        source_management_match
        or building_match
        or (address_match and _has_name_or_phone(left, right))
        or (distance is not None and distance <= 30 and name_similarity >= 0.80)
    )
    return MatchFeatures(
        source_management_match=source_management_match,
        official_record_identity=official_record_identity,
        building_match=building_match,
        address_match=address_match,
        address_unit_match=address_unit_match,
        coordinate_distance_metres=distance,
        name_similarity=name_similarity,
        phones_match=phones_match,
        one_phone_missing=one_phone_missing,
        phones_conflict=phones_conflict,
        is_candidate=is_candidate,
    )


def _latest_records(db: Database, run_id: UUID) -> list[dict[str, object]]:
    target_is_dated = bool(
        db.query("select 1 from pipeline_run where run_id = ?", [run_id])
    )
    completed = latest_complete_snapshot_runs(db, run_id)
    if target_is_dated and not completed:
        return []
    immutable_records = (
        latest_immutable_license_records(db, run_id, completed)
        if target_is_dated
        else None
    )
    if immutable_records is not None:
        records = immutable_records
    elif target_is_dated:
        require_immutable_for_overwritten_completed_runs(db, run_id, completed)
        raise RuntimeError(
            "immutable license revisions are required for a dated analytical run"
        )
    else:
        rows = db.query(
            """
            select source_id, source_record_id, source_name, normalized_name,
                   road_address, lot_address, district, region_group,
                   normalized_phone, longitude, latitude, status_code,
                   status_name, closure_date, observed_on, last_loaded_run_id
            from (
                select *, row_number() over (
                    partition by source_id, source_record_id
                    order by observed_on desc,
                             source_updated_at desc nulls last
                ) as row_number
                from staging_license_snapshot
            ) where row_number = 1
            """
        )
    if immutable_records is None:
        fields = (
            "source_id", "source_record_id", "source_name", "normalized_name", "road_address",
            "lot_address", "district", "region_group", "normalized_phone", "longitude", "latitude",
            "status_code", "status_name", "closure_date", "observed_on", "last_loaded_run_id",
        )
        records = [dict(zip(fields, row, strict=True)) for row in rows]
        for record in records:
            record["selected_version_run_id"] = record["last_loaded_run_id"]
            record["selected_observed_on"] = record["observed_on"]
            record["selected_revision_sequence"] = 1
    return [
        record
        for record in records
        if is_active_status(
            record["status_code"],
            record["status_name"],
            record["closure_date"],
            record["selected_observed_on"],
        )
    ]


def _visible_run_ids(db: Database, run_id: UUID) -> tuple[UUID, ...]:
    ensure_run_rebuildable(db, run_id)
    rows = db.query(
        """select lineage.input_run_id from pipeline_run_input as lineage
           left join pipeline_run as input on input.run_id = lineage.input_run_id
           where lineage.run_id = ?
           order by input.business_date nulls last, input.started_at nulls last,
                    lineage.input_run_id""",
        [run_id],
    )
    if rows:
        return tuple(row[0] for row in rows)
    if db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
        return (run_id,)
    legacy = db.query(
        "select distinct version_run_id from staging_license_revision order by version_run_id"
    )
    return tuple(row[0] for row in legacy) or (run_id,)


def _building_ids(db: Database, run_id: UUID) -> dict[str, set[UUID]]:
    result: dict[str, set[UUID]] = defaultdict(set)
    if db.query("select 1 from pipeline_run where run_id = ?", [run_id]):
        visible_runs = _visible_run_ids(db, run_id)
        placeholders = ",".join("?" for _ in visible_runs)
        rows = db.query(
            f"""with ranked_snapshot as (
                    select snapshot.*, row_number() over (
                        partition by snapshot.source_id, snapshot.source_record_id
                        order by producer.business_date desc nulls last,
                                 producer.started_at desc nulls last,
                                 snapshot.completed_at desc,
                                 snapshot.producer_run_id desc
                    ) as snapshot_rank
                    from run_license_building_snapshot as snapshot
                    join pipeline_run as producer
                      on producer.run_id = snapshot.producer_run_id
                    join pipeline_run as target on target.run_id = ?
                    where snapshot.producer_run_id in ({placeholders})
                      and producer.business_date <= target.business_date
                )
                select snapshot.source_id, snapshot.source_record_id,
                       observation.building_id
                from ranked_snapshot as snapshot
                left join run_license_building_observation as observation
                  on observation.run_id = snapshot.producer_run_id
                 and observation.source_id = snapshot.source_id
                 and observation.source_record_id = snapshot.source_record_id
                where snapshot.snapshot_rank = 1""",
            [run_id, *visible_runs],
        )
    else:
        rows = db.query(
            "select source_id, source_record_id, building_id from bridge_license_building"
        )
    for source_id, source_record_id, building_id in rows:
        if building_id is not None:
            result[f"{source_id}:{source_record_id}"].add(building_id)
    return result


def _adjudications(
    db: Database,
) -> dict[tuple[str, str], Literal["merge", "separate"]]:
    rows = db.query(
        """
        select left_registration_key, right_registration_key, decision
        from (
            select *, row_number() over (
                partition by left_registration_key, right_registration_key
                order by created_at desc, data_version desc
            ) as row_number
            from entity_pair_adjudication
            where algorithm_version = ?
        )
        where row_number = 1
        """,
        [ALGORITHM_VERSION],
    )
    return {
        (str(left), str(right)): decision
        for left, right, decision in rows
        if decision in {"merge", "separate"}
    }


def _default_calibration_allows_auto_merge() -> bool:
    # The bundled labeled pairs are a developer regression fixture, not a
    # representative production sample.  Automatic publication merges remain
    # disabled until a separately governed, versioned calibration is supplied.
    return False


def _safe_physical_merge_edges(
    records: list[dict[str, object]],
    decisions: list[tuple[dict[str, object], dict[str, object], MatchDecision]],
) -> tuple[
    list[tuple[str, str]],
    list[tuple[dict[str, object], dict[str, object], list[dict[str, object]]]],
]:
    """Accept only automatic edges that do not cross contradictory components."""
    parent = {_registration_key(record): _registration_key(record) for record in records}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    automatic = sorted(
        (
            (left, right)
            for left, right, decision in decisions
            if decision.label == "auto_merge"
        ),
        key=lambda pair: tuple(sorted((_registration_key(pair[0]), _registration_key(pair[1])))),
    )
    accepted: list[tuple[str, str]] = []
    blocked: list[tuple[dict[str, object], dict[str, object], list[dict[str, object]]]] = []
    for left, right in automatic:
        left_root = find(_registration_key(left))
        right_root = find(_registration_key(right))
        if left_root == right_root:
            continue
        witnesses: list[dict[str, object]] = []
        for witness_left, witness_right, witness_decision in decisions:
            witness_left_root = find(_registration_key(witness_left))
            witness_right_root = find(_registration_key(witness_right))
            spans_components = {
                witness_left_root,
                witness_right_root,
            } == {left_root, right_root}
            if spans_components and (
                witness_decision.label in {"review", "separate"}
                or witness_decision.features.phones_conflict
            ):
                witnesses.append(
                    {
                        "left_registration_key": _registration_key(witness_left),
                        "right_registration_key": _registration_key(witness_right),
                        "decision": witness_decision.label,
                        "features": asdict(witness_decision.features),
                    }
                )
        if witnesses:
            blocked.append((left, right, witnesses))
            continue
        parent[max(left_root, right_root)] = min(left_root, right_root)
        accepted.append((_registration_key(left), _registration_key(right)))
    return accepted, blocked


def _attach_designations(
    records: list[dict[str, object]],
    decisions: list[tuple[dict[str, object], dict[str, object], MatchDecision]],
    physical_components: dict[str, tuple[str, ...]],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    dict[str, str],
]:
    """Attach only unambiguous tourist-pension overlays to physical components."""
    designation_keys = {
        _registration_key(record) for record in records if _is_tourist_pension(record)
    }
    targets: dict[str, set[str]] = defaultdict(set)
    for left, right, decision in decisions:
        if decision.label != "designation_link":
            continue
        left_key = _registration_key(left)
        right_key = _registration_key(right)
        if left_key in designation_keys and right_key in physical_components:
            targets[left_key].add(right_key)
        if right_key in designation_keys and left_key in physical_components:
            targets[right_key].add(left_key)

    designation_targets: dict[str, tuple[str, ...]] = {}
    unmatched: dict[str, str] = {}
    for designation_key in designation_keys:
        target_components = {
            physical_components[target_key] for target_key in targets[designation_key]
        }
        if len(target_components) == 1:
            designation_targets[designation_key] = next(iter(target_components))
        elif not target_components:
            unmatched[designation_key] = "no_confident_physical_facility_match"
        else:
            unmatched[designation_key] = "ambiguous_physical_facility_match"

    return physical_components, designation_targets, unmatched


def _components(
    records: list[dict[str, object]], edges: list[tuple[str, str]]
) -> dict[str, tuple[str, ...]]:
    parent = {_registration_key(record): _registration_key(record) for record in records}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for left, right in edges:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)
    groups: dict[str, list[str]] = defaultdict(list)
    for key in parent:
        groups[find(key)].append(key)
    return {key: tuple(sorted(groups[find(key)])) for key in parent}


def _canonical_component_ids(
    db: Database,
    components: set[tuple[str, ...]],
    run_id: UUID,
    writer: Callable[[str, list[object] | None], None],
) -> dict[tuple[str, ...], UUID]:
    """Reuse a survivor identity as the observed registration component evolves."""
    previous_by_key = {
        f"{source_id}:{source_record_id}": facility_id
        for facility_id, source_id, source_record_id in db.query(
            "select facility_id, source_id, source_record_id from bridge_facility_license"
        )
    }
    aliases = {
        alias: canonical
        for alias, canonical in db.query(
            "select alias_facility_id, canonical_facility_id from facility_identity_alias"
        )
    }

    def canonical(facility_id: UUID) -> UUID:
        seen: set[UUID] = set()
        while facility_id in aliases and facility_id not in seen:
            seen.add(facility_id)
            facility_id = aliases[facility_id]
        return facility_id

    anchors = {
        facility_id: str(anchor)
        for facility_id, anchor in db.query(
            "select facility_id, anchor_registration_key from facility_identity_anchor"
        )
    }
    result: dict[tuple[str, ...], UUID] = {}
    assigned: set[UUID] = set()
    for component in sorted(components):
        candidates = {
            canonical(previous_by_key[key])
            for key in component
            if key in previous_by_key
        }
        available = candidates - assigned
        anchored = {
            facility_id
            for facility_id in available
            if anchors.get(facility_id) in component
        }
        if anchored:
            survivor = min(anchored, key=str)
        elif available:
            survivor = min(available, key=str)
        else:
            survivor = _facility_id((component[0],))
            if survivor in assigned:
                survivor = uuid5(
                    NAMESPACE_URL,
                    "facility-split:" + "|".join(component),
                )
        assigned.add(survivor)
        result[component] = survivor
        if survivor not in anchors:
            writer(
                """
                insert into facility_identity_anchor (
                    facility_id, anchor_registration_key, established_run_id
                ) values (?, ?, ?)
                on conflict do nothing
                """,
                [survivor, component[0], run_id],
            )
            anchors[survivor] = component[0]
        for losing_id in sorted(candidates - {survivor}, key=str):
            writer(
                """
                insert into facility_identity_alias (
                    alias_facility_id, canonical_facility_id, reason, created_run_id
                ) values (?, ?, 'component_merge_survivor', ?)
                on conflict (alias_facility_id) do nothing
                """,
                [losing_id, survivor, run_id],
            )
            aliases[losing_id] = survivor
    return result


def _facility_id(registration_keys: tuple[str, ...]) -> UUID:
    return uuid5(NAMESPACE_URL, "facility:" + "|".join(sorted(registration_keys)))


def _license_mapping(record: LicenseRecord) -> dict[str, object]:
    return {
        "source_id": record.source_id,
        "source_record_id": record.source_record_id,
        "normalized_name": record.normalized_name,
        "road_address": record.road_address,
        "lot_address": record.lot_address,
        "normalized_phone": record.normalized_phone,
        "longitude": record.longitude,
        "latitude": record.latitude,
    }


def _labeled_pair(row: Mapping[str, str]) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {key.removeprefix("left_"): value for key, value in row.items() if key.startswith("left_") and value},
        {key.removeprefix("right_"): value for key, value in row.items() if key.startswith("right_") and value},
    )


def _source_id(record: Mapping[str, object]) -> str | None:
    value = record.get("source_id", record.get("source"))
    return str(value) if value not in (None, "") else None


def _source_record_id(record: Mapping[str, object]) -> str | None:
    value = record.get("source_record_id", record.get("management_number"))
    return str(value) if value not in (None, "") else None


def _registration_key(record: Mapping[str, object]) -> str:
    source_id = _source_id(record)
    source_record_id = _source_record_id(record)
    if source_id is None or source_record_id is None:
        raise ValueError("facility records require source_id and source_record_id")
    return f"{source_id}:{source_record_id}"


def _normalized_name(record: Mapping[str, object]) -> str | None:
    value = record.get("normalized_name")
    return str(value) if value not in (None, "") else normalize_name(_text(record.get("name", record.get("source_name"))))


def _phone(record: Mapping[str, object]) -> str | None:
    value = record.get("normalized_phone")
    return str(value) if value not in (None, "") else normalize_phone(_text(record.get("phone")))


def _address_values(record: Mapping[str, object]) -> set[str]:
    values = (record.get("address"), record.get("road_address"), record.get("lot_address"))
    return {
        normalize_address(_text(value)).value.casefold()
        for value in values
        if normalize_address(_text(value)).value is not None
    }


def _address_unit_values(record: Mapping[str, object]) -> set[str]:
    """Return exact parsed floor/unit tokens; a bare street/parcel is insufficient."""
    patterns = (
        re.compile(r"(?<!\d)(\d{1,3})\s*층"),
        re.compile(r"(?<!\d)(\d{1,5})\s*호"),
    )
    units: set[str] = set()
    for value in (
        record.get("address"),
        record.get("road_address"),
        record.get("lot_address"),
    ):
        text = _text(value)
        if text is None:
            continue
        tokens = [match.group(1) for pattern in patterns for match in pattern.finditer(text)]
        if tokens:
            units.add("|".join(tokens))
    return units


def _building_values(record: Mapping[str, object]) -> set[str]:
    value = record.get("building_ids", record.get("building_id"))
    if value is None:
        return set()
    if isinstance(value, (set, frozenset, list, tuple)):
        return {str(item) for item in value if item is not None}
    return {str(value)}


def _has_name_or_phone(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return any((_normalized_name(left), _normalized_name(right), _phone(left), _phone(right)))


def _coordinate_distance(left: Mapping[str, object], right: Mapping[str, object]) -> float | None:
    try:
        left_longitude = float(left["longitude"])
        left_latitude = float(left["latitude"])
        right_longitude = float(right["longitude"])
        right_latitude = float(right["latitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (
        124 <= left_longitude <= 132
        and 33 <= left_latitude <= 39
        and 124 <= right_longitude <= 132
        and 33 <= right_latitude <= 39
    ):
        return None
    latitude_delta = math.radians(right_latitude - left_latitude)
    longitude_delta = math.radians(right_longitude - left_longitude)
    a = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(math.radians(left_latitude))
        * math.cos(math.radians(right_latitude))
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_tourist_pension(record: Mapping[str, object]) -> bool:
    return _source_id(record) == "tourist_pensions"


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None

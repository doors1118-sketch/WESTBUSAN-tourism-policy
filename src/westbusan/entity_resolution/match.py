"""Conservative, deterministic resolution of legal registrations into facilities."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from rapidfuzz.fuzz import ratio

from westbusan.accommodation.normalize import LicenseRecord
from westbusan.db import Database
from westbusan.entity_resolution.normalize import (
    normalize_address,
    normalize_name,
    normalize_phone,
)

DecisionLabel = Literal["auto_merge", "designation_link", "review", "separate"]


@dataclass(frozen=True, slots=True)
class MatchFeatures:
    """Evidence calculated without making a merge decision."""

    source_management_match: bool
    official_record_identity: bool
    building_match: bool
    address_match: bool
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
        and (features.phones_match or features.one_phone_missing)
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
    return true_positive / predicted_positive if predicted_positive else 0.0


def build_facilities(db: Database, run_id: UUID) -> FacilityBuildResult:
    """Build physical facilities from latest snapshots, preserving every registration."""
    records = _latest_records(db)
    building_ids = _building_ids(db)
    for record in records:
        record["building_ids"] = building_ids.get(_registration_key(record), set())

    decisions: list[tuple[dict[str, object], dict[str, object], MatchDecision]] = []
    merge_edges: list[tuple[str, str]] = []
    for left, right in combinations(records, 2):
        decision = classify_pair(left, right)
        if not decision.features.is_candidate:
            continue
        decisions.append((left, right, decision))
        if decision.label in {"auto_merge", "designation_link"}:
            merge_edges.append((_registration_key(left), _registration_key(right)))

    components = _components(records, merge_edges)
    evidence_by_key: dict[str, list[dict[str, object]]] = defaultdict(list)
    review_rows: list[tuple[UUID, UUID, UUID, str]] = []
    for left, right, decision in decisions:
        evidence = {
            "decision": decision.label,
            "left_registration_key": _registration_key(left),
            "right_registration_key": _registration_key(right),
            "features": asdict(decision.features),
        }
        if decision.label in {"auto_merge", "designation_link"}:
            evidence_by_key[_registration_key(left)].append(evidence)
            evidence_by_key[_registration_key(right)].append(evidence)
        elif decision.label == "review":
            left_id = _facility_id(components[_registration_key(left)])
            right_id = _facility_id(components[_registration_key(right)])
            review_rows.append(
                (
                    uuid5(
                        NAMESPACE_URL,
                        "duplicate-review:"
                        + "|".join(sorted((_registration_key(left), _registration_key(right)))),
                    ),
                    left_id,
                    right_id,
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                )
            )

    db.connection.execute("delete from bridge_facility_license")
    db.connection.execute("delete from bridge_facility_building")
    db.connection.execute("delete from dim_facility")
    for component in {tuple(keys) for keys in components.values()}:
        facility_id = _facility_id(component)
        component_records = [record for record in records if _registration_key(record) in component]
        canonical = next(
            (record["source_name"] for record in component_records if record["source_name"]),
            None,
        )
        district = next((record["district"] for record in component_records if record["district"]), None)
        region_group = next(
            (record["region_group"] for record in component_records if record["region_group"]),
            None,
        )
        db.connection.execute(
            """
            insert into dim_facility (facility_id, canonical_name, district, region_group)
            values (?, ?, ?, ?)
            """,
            [facility_id, canonical, district, region_group],
        )
        for record in component_records:
            key = _registration_key(record)
            evidence = json.dumps(
                {"registration_key": key, "merge_evidence": evidence_by_key[key]},
                ensure_ascii=False,
                sort_keys=True,
            )
            db.connection.execute(
                """
                insert into bridge_facility_license
                    (facility_id, source_id, source_record_id, evidence_json)
                values (?, ?, ?, ?)
                """,
                [facility_id, record["source_id"], record["source_record_id"], evidence],
            )
            for building_id in record["building_ids"]:
                db.connection.execute(
                    """
                    insert into bridge_facility_building (facility_id, building_id)
                    values (?, ?)
                    on conflict do nothing
                    """,
                    [facility_id, building_id],
                )

    for review_id, left_id, right_id, evidence in review_rows:
        db.connection.execute(
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

    return FacilityBuildResult(
        facility_count=len({tuple(keys) for keys in components.values()}),
        license_links=len(records),
        review_pairs=len(review_rows),
        designation_links=sum(
            decision.label == "designation_link" for _, _, decision in decisions
        ),
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
        coordinate_distance_metres=distance,
        name_similarity=name_similarity,
        phones_match=phones_match,
        one_phone_missing=one_phone_missing,
        phones_conflict=phones_conflict,
        is_candidate=is_candidate,
    )


def _latest_records(db: Database) -> list[dict[str, object]]:
    rows = db.query(
        """
        select source_id, source_record_id, source_name, normalized_name, road_address,
               lot_address, district, region_group, normalized_phone, longitude, latitude
        from (
            select *, row_number() over (
                partition by source_id, source_record_id
                order by observed_on desc, source_updated_at desc nulls last
            ) as row_number
            from staging_license_snapshot
        )
        where row_number = 1
        """
    )
    fields = (
        "source_id", "source_record_id", "source_name", "normalized_name", "road_address",
        "lot_address", "district", "region_group", "normalized_phone", "longitude", "latitude",
    )
    return [dict(zip(fields, row, strict=True)) for row in rows]


def _building_ids(db: Database) -> dict[str, set[UUID]]:
    result: dict[str, set[UUID]] = defaultdict(set)
    for source_id, source_record_id, building_id in db.query(
        "select source_id, source_record_id, building_id from bridge_license_building"
    ):
        result[f"{source_id}:{source_record_id}"].add(building_id)
    return result


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

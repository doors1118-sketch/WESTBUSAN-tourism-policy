"""PNU-bound planning designations for the separate Nakdong River review tab.

The catalogue is an immutable, approved snapshot.  It provides a conservative
pre-screen only and never treats missing data as an absence of regulation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import duckdb

from westbusan.db import Database
from westbusan.vacant_house.parcel_context import (
    VWorldNedParcelContextClient,
    normalize_land_characteristics,
)

_PNU = re.compile(r"^\d{19}$")

DesignationCategory = Literal[
    "development_restriction",
    "district_unit_plan",
    "urban_planning_facility",
    "land_use_zone",
    "land_use_district",
    "land_use_area",
    "other_law_restriction",
    "unclassified",
]


def classify_designation(name: str) -> DesignationCategory:
    """Classify special legal controls before generic Korean suffixes."""
    value = " ".join(str(name).split())
    if "개발행위허가제한" in value:
        return "development_restriction"
    if "지구단위계획" in value:
        return "district_unit_plan"
    if "도시계획시설" in value or "도시·군계획시설" in value:
        return "urban_planning_facility"
    if value.endswith("지역"):
        return "land_use_zone"
    if value.endswith("지구"):
        return "land_use_district"
    if value.endswith("구역"):
        return "land_use_area"
    if any(token in value for token in ("보호", "제한", "보전", "허가")):
        return "other_law_restriction"
    return "unclassified"


@dataclass(frozen=True, slots=True)
class PlanningDesignation:
    name: str
    category: DesignationCategory


@dataclass(frozen=True, slots=True)
class ParcelCharacteristics:
    land_category: str | None = None
    parcel_area: float | None = None
    road_side: str | None = None
    terrain_height: str | None = None
    terrain_shape: str | None = None
    land_use_situation: str | None = None


@dataclass(frozen=True, slots=True)
class ParcelPlanningReview:
    status: str
    complete: bool
    grade: str
    label: str
    reason: str
    next_check: str
    pnu: str | None
    snapshot_id: str | None
    checked_at: str | None
    source_date: str | None = None
    designations: tuple[PlanningDesignation, ...] = ()
    characteristics: ParcelCharacteristics | None = None
    legal_effect: bool = False

    def as_public_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["designations"] = [asdict(item) for item in self.designations]
        value["disclaimer"] = (
            "토지이용 규제 속성의 승인 스냅샷을 이용한 사전검토이며 인허가 처분이 "
            "아닙니다. 최신 토지이용계획확인서, 개별 고시와 관리청 의견으로 재확인해야 합니다."
        )
        return value


@dataclass(frozen=True, slots=True)
class _ParcelRecord:
    pnu: str
    designations: tuple[PlanningDesignation, ...]
    characteristics: ParcelCharacteristics | None
    source_date: str | None


class NakdongParcelCatalogue:
    """Read-only planning catalogue used only by the Nakdong River endpoint."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        checked_at: str,
        parcels: Mapping[str, _ParcelRecord],
    ) -> None:
        self.snapshot_id = snapshot_id
        self.checked_at = checked_at
        self._parcels = dict(parcels)

    @classmethod
    def from_records(
        cls,
        *,
        snapshot_id: str,
        checked_at: str,
        parcels: Sequence[Mapping[str, object]],
    ) -> NakdongParcelCatalogue:
        parsed: dict[str, _ParcelRecord] = {}
        for record in parcels:
            pnu = str(record.get("pnu") or "")
            if _PNU.fullmatch(pnu) is None:
                raise ValueError("invalid_pnu")
            if pnu in parsed:
                raise ValueError("duplicate_pnu")
            if record.get("land_use_status") != "matched":
                raise ValueError("land_use_coverage_must_be_complete")
            names = _designation_names(record.get("land_use_designations"))
            characteristics_value = record.get("land_characteristics")
            characteristics = (
                ParcelCharacteristics(**characteristics_value)
                if isinstance(characteristics_value, dict)
                else None
            )
            parsed[pnu] = _ParcelRecord(
                pnu=pnu,
                designations=tuple(
                    PlanningDesignation(name=name, category=classify_designation(name))
                    for name in names
                ),
                characteristics=characteristics,
                source_date=(
                    str(record["source_date"])
                    if record.get("source_date") not in (None, "")
                    else None
                ),
            )
        if not parsed:
            raise ValueError("nakdong_parcel_snapshot_must_not_be_empty")
        return cls(snapshot_id=snapshot_id, checked_at=checked_at, parcels=parsed)

    def review_pnu(self, *, pnu: str, activity: str) -> ParcelPlanningReview:
        if _PNU.fullmatch(pnu) is None:
            raise ValueError("invalid_pnu")
        record = self._parcels.get(pnu)
        if record is None:
            return _unavailable_review(
                status="pnu_not_published",
                pnu=pnu,
                label="해당 필지 미발행·미판정",
                reason="현재 승인된 낙동강 필지 규제 스냅샷에 해당 PNU가 없습니다.",
                next_check="검토대상 PNU 목록에 추가해 공식 토지이용 속성을 동기화하십시오.",
                snapshot_id=self.snapshot_id,
                checked_at=self.checked_at,
            )
        categories = {item.category for item in record.designations}
        names = {item.name for item in record.designations}
        if "development_restriction" in categories:
            grade = "principally_restricted"
            label = "개발행위 제한 중첩·원칙적 제약"
            reason = "개발행위허가제한 관련 지정이 확인되어 일반적인 입지검토보다 우선합니다."
            next_check = "제한지역 지정 고시의 대상행위·기간·예외와 관할 도시계획부서 의견을 확인하십시오."
        elif "urban_planning_facility" in categories:
            grade = "principally_restricted"
            label = "도시계획시설 중첩·대체입지 우선검토"
            reason = "도시계획시설 관련 지정이 확인되어 시설사업과의 양립 가능성을 먼저 확인해야 합니다."
            next_check = "도시관리계획 결정도서, 시설사업 시행 여부와 관리부서 협의사항을 확인하십시오."
        elif "district_unit_plan" in categories:
            grade = "conditional"
            label = "지구단위계획 지침 확인 필요"
            reason = "지구단위계획구역이 확인되어 용도·밀도·배치·경관 지침의 상세 적용을 받습니다."
            next_check = "지구단위계획 결정도서와 건축물 용도·건폐율·용적률·높이·주차 기준을 대조하십시오."
        elif activity in {"lodging", "food", "culture", "parking"} and any(
            token in name
            for name in names
            for token in ("자연환경보전지역", "보전녹지지역", "농림지역")
        ):
            grade = "conditional"
            label = "보전계열 용도지역·강화검토"
            reason = "보전계열 용도지역이 확인되어 건축·형질변경과 관광시설 입지 가능성의 별도 검토가 필요합니다."
            next_check = "국토계획법, 부산시 도시계획조례와 개발행위허가 기준을 사업규모별로 확인하십시오."
        else:
            grade = "conditional"
            label = "도시계획 상세기준 검토 가능"
            reason = "공식 토지이용 지정은 확인했으나 지정명만으로 사업 허용 여부를 확정할 수 없습니다."
            next_check = "토지이용계획확인서와 해당 지역·지구·구역의 행위제한, 건축물 용도를 대조하십시오."
        return ParcelPlanningReview(
            status="matched",
            complete=True,
            grade=grade,
            label=label,
            reason=reason,
            next_check=next_check,
            pnu=pnu,
            snapshot_id=self.snapshot_id,
            checked_at=self.checked_at,
            source_date=record.source_date,
            designations=record.designations,
            characteristics=record.characteristics,
        )


def collect_nakdong_parcel_records(
    pnus: Sequence[str],
    *,
    land_use_client: VWorldNedParcelContextClient,
    land_characteristics_client: VWorldNedParcelContextClient,
) -> tuple[dict[str, object], ...]:
    """Collect exact-PNU evidence without publishing partial land-use results."""
    unique = tuple(dict.fromkeys(str(pnu).strip() for pnu in pnus))
    if not unique or any(_PNU.fullmatch(pnu) is None for pnu in unique):
        raise ValueError("invalid_pnu_list")
    records: list[dict[str, object]] = []
    for pnu in unique:
        land_use = land_use_client.fetch(pnu)
        characteristics_fetch = land_characteristics_client.fetch(pnu)
        names_value = land_use.properties.get("landUseDesignations", [])
        names = list(_designation_names(names_value)) if land_use.status == "matched" else []
        characteristics = None
        if characteristics_fetch.status == "matched":
            characteristics = asdict(
                normalize_land_characteristics(characteristics_fetch.properties)
            )
        source_dates = [
            item.source_date
            for item in (land_use, characteristics_fetch)
            if item.source_date is not None
        ]
        records.append(
            {
                "pnu": pnu,
                "land_use_status": land_use.status,
                "land_use_designations": names,
                "land_use_response_sha256": land_use.response_sha256,
                "land_characteristics_status": characteristics_fetch.status,
                "land_characteristics_response_sha256": (
                    characteristics_fetch.response_sha256
                ),
                "land_characteristics": characteristics,
                "source_date": max(source_dates).isoformat() if source_dates else None,
            }
        )
    return tuple(records)


def pnu_required_review() -> ParcelPlanningReview:
    return _unavailable_review(
        status="pnu_required",
        pnu=None,
        label="주소·지번 입력 필요",
        reason="지도 좌표만으로는 필지별 용도지역·지구·구역을 확정하지 않습니다.",
        next_check="부산 주소 또는 지번을 검색해 PNU를 확인하십시오.",
    )


def catalogue_unavailable_review(pnu: str | None) -> ParcelPlanningReview:
    return _unavailable_review(
        status="catalogue_unavailable",
        pnu=pnu,
        label="낙동강 필지 규제 DB 미발행",
        reason="승인된 낙동강 전용 필지 규제 스냅샷을 불러오지 못했습니다.",
        next_check="PNU별 토지이용 속성 동기화와 최신 발행 포인터를 확인하십시오.",
    )


def _unavailable_review(
    *,
    status: str,
    pnu: str | None,
    label: str,
    reason: str,
    next_check: str,
    snapshot_id: str | None = None,
    checked_at: str | None = None,
) -> ParcelPlanningReview:
    return ParcelPlanningReview(
        status=status,
        complete=False,
        grade="unreviewed",
        label=label,
        reason=reason,
        next_check=next_check,
        pnu=pnu,
        snapshot_id=snapshot_id,
        checked_at=checked_at,
    )


def publish_nakdong_parcel_snapshot(
    db: Database,
    *,
    run_id: UUID,
    checked_at: datetime,
    parcels: Sequence[Mapping[str, object]],
) -> None:
    """Publish only a complete land-use set; keep the prior pointer on failure."""
    catalogue = NakdongParcelCatalogue.from_records(
        snapshot_id=str(run_id), checked_at=checked_at.isoformat(), parcels=parcels
    )
    del catalogue
    canonical = json.dumps(
        list(parcels), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    designation_count = sum(
        len(_designation_names(record.get("land_use_designations")))
        for record in parcels
    )
    connection = db.connection
    try:
        connection.execute("begin transaction")
        for record in parcels:
            pnu = str(record["pnu"])
            characteristics = record.get("land_characteristics")
            connection.execute(
                """insert into nakdong_parcel_regulation_snapshot (
                       run_id, pnu, land_use_status, land_use_response_sha256,
                       land_characteristics_status, land_characteristics_response_sha256,
                       land_characteristics_json, source_date
                   ) values (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    run_id,
                    pnu,
                    record["land_use_status"],
                    _sha256(record.get("land_use_response_sha256")),
                    str(record.get("land_characteristics_status") or "not_found"),
                    _sha256(record.get("land_characteristics_response_sha256")),
                    _json(characteristics) if characteristics is not None else None,
                    record.get("source_date"),
                ],
            )
            for order, name in enumerate(
                _designation_names(record.get("land_use_designations")), start=1
            ):
                connection.execute(
                    """insert into nakdong_parcel_designation_snapshot (
                           run_id, pnu, designation_order, designation_name,
                           designation_category
                       ) values (?, ?, ?, ?, ?)""",
                    [run_id, pnu, order, name, classify_designation(name)],
                )
        connection.execute(
            """insert into nakdong_parcel_regulation_sync_run (
                   run_id, checked_at, completed_at, parcel_count, designation_count,
                   source_name, source_url, content_hash, status
               ) values (?, ?, current_timestamp, ?, ?, ?, ?, ?, 'PUBLISHED')""",
            [
                run_id,
                checked_at.isoformat(),
                len(parcels),
                designation_count,
                "VWorld NED 토지이용속성",
                "https://api.vworld.kr/ned/data/getLandUseAttr",
                content_hash,
            ],
        )
        connection.execute(
            """insert into nakdong_parcel_regulation_publication_current (
                   publication_key, run_id, published_at
               ) values ('current', ?, current_timestamp)
               on conflict (publication_key) do update set
                 run_id=excluded.run_id, published_at=excluded.published_at""",
            [run_id],
        )
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise


def load_nakdong_parcel_catalogue(
    connection: duckdb.DuckDBPyConnection,
) -> NakdongParcelCatalogue | None:
    current = connection.execute(
        """select publication.run_id, run.checked_at::varchar
           from nakdong_parcel_regulation_publication_current as publication
           join nakdong_parcel_regulation_sync_run as run
             on run.run_id=publication.run_id
           where publication.publication_key='current' and run.status='PUBLISHED'"""
    ).fetchone()
    if current is None:
        return None
    run_id, checked_at = current
    parcel_rows = connection.execute(
        """select pnu, land_use_status, land_use_response_sha256,
                  land_characteristics_status, land_characteristics_response_sha256,
                  land_characteristics_json, source_date
           from nakdong_parcel_regulation_snapshot where run_id=? order by pnu""",
        [run_id],
    ).fetchall()
    designation_rows = connection.execute(
        """select pnu, designation_name from nakdong_parcel_designation_snapshot
           where run_id=? order by pnu, designation_order""",
        [run_id],
    ).fetchall()
    by_pnu: dict[str, list[str]] = {}
    for pnu, name in designation_rows:
        by_pnu.setdefault(str(pnu), []).append(str(name))
    records: list[dict[str, object]] = []
    for (
        pnu,
        land_use_status,
        land_use_hash,
        characteristic_status,
        characteristic_hash,
        characteristics_json,
        source_date,
    ) in parcel_rows:
        records.append(
            {
                "pnu": str(pnu),
                "land_use_status": str(land_use_status),
                "land_use_designations": by_pnu.get(str(pnu), []),
                "land_use_response_sha256": str(land_use_hash),
                "land_characteristics_status": str(characteristic_status),
                "land_characteristics_response_sha256": str(characteristic_hash),
                "land_characteristics": (
                    json.loads(characteristics_json)
                    if characteristics_json is not None
                    else None
                ),
                "source_date": str(source_date) if source_date is not None else None,
            }
        )
    return NakdongParcelCatalogue.from_records(
        snapshot_id=str(run_id), checked_at=str(checked_at), parcels=records
    )


def _designation_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("land_use_designations_required")
    return tuple(
        sorted({" ".join(str(item).split()) for item in value if str(item).strip()})
    )


def _sha256(value: object) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError("invalid_response_sha256")
    return text


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "NakdongParcelCatalogue",
    "ParcelCharacteristics",
    "ParcelPlanningReview",
    "PlanningDesignation",
    "catalogue_unavailable_review",
    "classify_designation",
    "collect_nakdong_parcel_records",
    "load_nakdong_parcel_catalogue",
    "pnu_required_review",
    "publish_nakdong_parcel_snapshot",
]

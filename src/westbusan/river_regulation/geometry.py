"""Immutable cadastral geometry snapshot for click-to-PNU resolution.

The geometry catalogue is deliberately independent from planning attributes.
It resolves only the approved target parcels and fails closed when a point is
outside the publication or falls on a shared parcel boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import duckdb
from shapely import from_wkb, normalize, to_wkb
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from westbusan.db import Database
from westbusan.vacant_house.cadastral import VWorldCadastralClient

_PNU = re.compile(r"^\d{19}$")
_CRS = "EPSG:4326"
_PROVIDER_STATUSES = frozenset(
    {"matched", "not_found", "provider_error", "invalid_response"}
)

ResolutionStatus = Literal[
    "matched",
    "boundary_ambiguous",
    "scope_not_published",
    "catalogue_unavailable",
    "provided",
]


@dataclass(frozen=True, slots=True)
class ParcelGeometryResolution:
    status: ResolutionStatus
    pnu: str | None
    candidate_pnus: tuple[str, ...]
    snapshot_id: str | None
    checked_at: str | None
    target_count: int
    matched_count: int
    complete: bool

    def as_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "pnu": self.pnu,
            "candidate_pnus": list(self.candidate_pnus),
            "snapshot_id": self.snapshot_id,
            "checked_at": self.checked_at,
            "target_count": self.target_count,
            "matched_count": self.matched_count,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class _GeometryRecord:
    pnu: str
    status: str
    request_identity: str
    response_sha256: str
    geometry: BaseGeometry | None
    geometry_hash: str | None
    source_date: str | None


class NakdongParcelGeometryCatalogue:
    """Read-only spatial index over one approved cadastral publication."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        checked_at: str,
        records: Sequence[_GeometryRecord],
    ) -> None:
        self.snapshot_id = snapshot_id
        self.checked_at = checked_at
        self.target_count = len(records)
        matched = tuple(record for record in records if record.status == "matched")
        self.matched_count = len(matched)
        self._pnus = tuple(record.pnu for record in matched)
        self._geometries = tuple(
            record.geometry for record in matched if record.geometry is not None
        )
        self._tree = STRtree(self._geometries) if self._geometries else None

    @classmethod
    def from_records(
        cls,
        *,
        snapshot_id: str,
        checked_at: str,
        records: Sequence[Mapping[str, object]],
    ) -> NakdongParcelGeometryCatalogue:
        parsed: list[_GeometryRecord] = []
        seen: set[str] = set()
        for value in records:
            pnu = str(value.get("pnu") or "")
            status = str(value.get("status") or "")
            if _PNU.fullmatch(pnu) is None:
                raise ValueError("invalid_pnu")
            if pnu in seen:
                raise ValueError("duplicate_pnu")
            seen.add(pnu)
            if status not in _PROVIDER_STATUSES:
                raise ValueError("invalid_geometry_provider_status")
            geometry_value = value.get("geometry")
            geometry: BaseGeometry | None = (
                normalize(geometry_value)
                if isinstance(geometry_value, BaseGeometry)
                else None
            )
            if status == "matched" and not _valid_geometry(geometry):
                raise ValueError("matched_geometry_required")
            if status != "matched" and geometry is not None:
                raise ValueError("unmatched_geometry_must_be_empty")
            geometry_hash = _geometry_hash(geometry) if geometry is not None else None
            parsed.append(
                _GeometryRecord(
                    pnu=pnu,
                    status=status,
                    request_identity=str(value.get("request_identity") or ""),
                    response_sha256=_sha256(value.get("response_sha256")),
                    geometry=geometry,
                    geometry_hash=geometry_hash,
                    source_date=(
                        str(value["source_date"])
                        if value.get("source_date") not in (None, "")
                        else None
                    ),
                )
            )
        if not parsed:
            raise ValueError("nakdong_parcel_geometry_snapshot_must_not_be_empty")
        return cls(snapshot_id=snapshot_id, checked_at=checked_at, records=parsed)

    def resolve(self, *, longitude: float, latitude: float) -> ParcelGeometryResolution:
        if not (128.0 <= longitude <= 130.0 and 34.0 <= latitude <= 36.0):
            raise ValueError("point_outside_busan_review_bounds")
        if self._tree is None:
            candidates: tuple[str, ...] = ()
        else:
            point = Point(longitude, latitude)
            indices = self._tree.query(point)
            candidates = tuple(
                sorted(
                    self._pnus[int(index)]
                    for index in indices
                    if self._geometries[int(index)].covers(point)
                )
            )
        if len(candidates) == 1:
            status: ResolutionStatus = "matched"
            pnu: str | None = candidates[0]
        elif len(candidates) > 1:
            status = "boundary_ambiguous"
            pnu = None
        else:
            status = "scope_not_published"
            pnu = None
        return ParcelGeometryResolution(
            status=status,
            pnu=pnu,
            candidate_pnus=candidates,
            snapshot_id=self.snapshot_id,
            checked_at=self.checked_at,
            target_count=self.target_count,
            matched_count=self.matched_count,
            complete=status == "matched",
        )

    def provided(self, pnu: str) -> ParcelGeometryResolution:
        if _PNU.fullmatch(pnu) is None:
            raise ValueError("invalid_pnu")
        return ParcelGeometryResolution(
            status="provided",
            pnu=pnu,
            candidate_pnus=(pnu,),
            snapshot_id=self.snapshot_id,
            checked_at=self.checked_at,
            target_count=self.target_count,
            matched_count=self.matched_count,
            complete=True,
        )


def unavailable_geometry_resolution() -> ParcelGeometryResolution:
    return ParcelGeometryResolution(
        status="catalogue_unavailable",
        pnu=None,
        candidate_pnus=(),
        snapshot_id=None,
        checked_at=None,
        target_count=0,
        matched_count=0,
        complete=False,
    )


def collect_nakdong_parcel_geometries(
    pnus: Sequence[str],
    *,
    client: VWorldCadastralClient,
) -> tuple[dict[str, object], ...]:
    """Fetch every target PNU and retain provider failures for coverage audit."""
    unique = tuple(dict.fromkeys(str(pnu).strip() for pnu in pnus))
    if not unique or any(_PNU.fullmatch(pnu) is None for pnu in unique):
        raise ValueError("invalid_pnu_list")
    records: list[dict[str, object]] = []
    for pnu in unique:
        fetch = client.fetch(pnu)
        records.append(
            {
                "pnu": pnu,
                "status": fetch.status,
                "request_identity": fetch.request_identity,
                "response_sha256": fetch.response_sha256,
                "geometry": fetch.geometry,
                "geometry_hash": fetch.geometry_hash,
                "source_date": (
                    fetch.source_date.isoformat() if fetch.source_date is not None else None
                ),
            }
        )
    return tuple(records)


def load_current_nakdong_regulation_pnus(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[str, ...]:
    """Return the immutable membership of the current approved parcel publication."""
    rows = connection.execute(
        """select snapshot.pnu
           from nakdong_parcel_regulation_publication_current as publication
           join nakdong_parcel_regulation_snapshot as snapshot
             on snapshot.run_id=publication.run_id
           where publication.publication_key='current'
           order by snapshot.pnu"""
    ).fetchall()
    pnus = tuple(str(row[0]) for row in rows)
    if not pnus:
        raise ValueError("nakdong_parcel_regulation_publication_missing")
    if len(pnus) != len(set(pnus)) or any(_PNU.fullmatch(pnu) is None for pnu in pnus):
        raise ValueError("nakdong_parcel_regulation_membership_invalid")
    return pnus


def publish_nakdong_parcel_geometry_snapshot(
    db: Database,
    *,
    run_id: UUID,
    checked_at: datetime,
    records: Sequence[Mapping[str, object]],
) -> None:
    """Atomically publish a complete target list, including provider failures."""
    catalogue = NakdongParcelGeometryCatalogue.from_records(
        snapshot_id=str(run_id),
        checked_at=checked_at.isoformat(),
        records=records,
    )
    normalized = _normalized_records(records)
    counts = {status: 0 for status in _PROVIDER_STATUSES}
    for record in normalized:
        counts[str(record["status"])] += 1
    canonical = json.dumps(
        [
            {
                key: value
                for key, value in record.items()
                if key not in {"geometry_wkb"}
            }
            for record in normalized
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    connection = db.connection
    try:
        connection.execute("begin transaction")
        for record in normalized:
            connection.execute(
                """insert into nakdong_parcel_geometry_snapshot (
                       run_id, pnu, provider_status, request_identity,
                       response_sha256, geometry_wkb, geometry_sha256,
                       minimum_longitude, minimum_latitude,
                       maximum_longitude, maximum_latitude, source_date
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    run_id,
                    record["pnu"],
                    record["status"],
                    record["request_identity"],
                    record["response_sha256"],
                    record["geometry_wkb"],
                    record["geometry_hash"],
                    record["minimum_longitude"],
                    record["minimum_latitude"],
                    record["maximum_longitude"],
                    record["maximum_latitude"],
                    record["source_date"],
                ],
            )
        connection.execute(
            """insert into nakdong_parcel_geometry_sync_run (
                   run_id, checked_at, completed_at, target_count, matched_count,
                   not_found_count, provider_error_count, invalid_response_count,
                   source_name, source_url, crs, content_hash, status
               ) values (?, ?, current_timestamp, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         'PUBLISHED')""",
            [
                run_id,
                checked_at.isoformat(),
                catalogue.target_count,
                counts["matched"],
                counts["not_found"],
                counts["provider_error"],
                counts["invalid_response"],
                "VWorld 연속지적도",
                "https://api.vworld.kr/req/data",
                _CRS,
                content_hash,
            ],
        )
        connection.execute(
            """insert into nakdong_parcel_geometry_publication_current (
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


def load_nakdong_parcel_geometry_catalogue(
    connection: duckdb.DuckDBPyConnection,
) -> NakdongParcelGeometryCatalogue | None:
    current = connection.execute(
        """select publication.run_id, run.checked_at::varchar
           from nakdong_parcel_geometry_publication_current as publication
           join nakdong_parcel_geometry_sync_run as run
             on run.run_id=publication.run_id
           where publication.publication_key='current' and run.status='PUBLISHED'"""
    ).fetchone()
    if current is None:
        return None
    run_id, checked_at = current
    rows = connection.execute(
        """select pnu, provider_status, request_identity, response_sha256,
                  geometry_wkb, geometry_sha256, source_date
           from nakdong_parcel_geometry_snapshot
           where run_id=? order by pnu""",
        [run_id],
    ).fetchall()
    records: list[dict[str, object]] = []
    for pnu, status, request_identity, response_hash, wkb, geometry_hash, source_date in rows:
        records.append(
            {
                "pnu": str(pnu),
                "status": str(status),
                "request_identity": str(request_identity),
                "response_sha256": str(response_hash),
                "geometry": from_wkb(bytes(wkb)) if wkb is not None else None,
                "geometry_hash": str(geometry_hash) if geometry_hash is not None else None,
                "source_date": str(source_date) if source_date is not None else None,
            }
        )
    return NakdongParcelGeometryCatalogue.from_records(
        snapshot_id=str(run_id), checked_at=str(checked_at), records=records
    )


def _normalized_records(
    records: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    parsed = NakdongParcelGeometryCatalogue.from_records(
        snapshot_id="validation",
        checked_at="validation",
        records=records,
    )
    del parsed
    normalized_records: list[dict[str, object]] = []
    for value in records:
        geometry_value = value.get("geometry")
        geometry = (
            normalize(geometry_value)
            if isinstance(geometry_value, BaseGeometry)
            else None
        )
        bounds: tuple[float | None, float | None, float | None, float | None]
        if geometry is None:
            bounds = (None, None, None, None)
            geometry_wkb = None
            geometry_hash = None
        else:
            bounds = geometry.bounds
            geometry_wkb = to_wkb(
                geometry, byte_order=1, output_dimension=2, include_srid=False
            )
            geometry_hash = hashlib.sha256(geometry_wkb).hexdigest()
        normalized_records.append(
            {
                "pnu": str(value["pnu"]),
                "status": str(value["status"]),
                "request_identity": str(value.get("request_identity") or ""),
                "response_sha256": _sha256(value.get("response_sha256")),
                "geometry_wkb": geometry_wkb,
                "geometry_hash": geometry_hash,
                "minimum_longitude": bounds[0],
                "minimum_latitude": bounds[1],
                "maximum_longitude": bounds[2],
                "maximum_latitude": bounds[3],
                "source_date": value.get("source_date"),
            }
        )
    return tuple(sorted(normalized_records, key=lambda item: str(item["pnu"])))


def _valid_geometry(geometry: BaseGeometry | None) -> bool:
    return bool(
        geometry is not None
        and geometry.geom_type in {"Polygon", "MultiPolygon"}
        and not geometry.is_empty
        and geometry.is_valid
        and geometry.area > 0
    )


def _geometry_hash(geometry: BaseGeometry) -> str:
    value = to_wkb(geometry, byte_order=1, output_dimension=2, include_srid=False)
    return hashlib.sha256(value).hexdigest()


def _sha256(value: object) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError("invalid_sha256")
    return text


__all__ = [
    "NakdongParcelGeometryCatalogue",
    "ParcelGeometryResolution",
    "collect_nakdong_parcel_geometries",
    "load_current_nakdong_regulation_pnus",
    "load_nakdong_parcel_geometry_catalogue",
    "publish_nakdong_parcel_geometry_snapshot",
    "unavailable_geometry_resolution",
]

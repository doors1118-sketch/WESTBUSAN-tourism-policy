"""Load normalized Busan accommodation snapshots into DuckDB."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from uuid import UUID

from westbusan.accommodation.normalize import LicenseRecord
from westbusan.db import Database


def _payload(record: LicenseRecord) -> tuple[list[object], str]:
    source_payload = json.dumps(
        record.source_payload_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    values: list[object] = [
        record.source_id,
        record.source_record_id,
        record.observed_on,
        record.jurisdiction_code,
        record.source_name,
        record.normalized_name,
        record.road_address,
        record.lot_address,
        record.district,
        record.region_group,
        record.region_quality,
        record.license_date,
        record.closure_date,
        record.status_code,
        record.status_name,
        record.status_class,
        record.detailed_status_code,
        record.detailed_status_name,
        record.room_count,
        record.room_count_quality,
        record.normalized_phone,
        record.longitude,
        record.latitude,
        record.projected_x,
        record.projected_y,
        record.coordinate_crs,
        record.source_updated_at,
        record.data_updated_on,
        record.data_update_point,
        source_payload,
    ]
    encoded = json.dumps(values, ensure_ascii=False, default=str, separators=(",", ":"))
    return values, hashlib.sha256(encoded.encode()).hexdigest()


_INSERT_SQL = """
insert into staging_license_snapshot (
    source_id, source_record_id, observed_on, first_loaded_run_id, last_loaded_run_id,
    jurisdiction_code, source_name,
    normalized_name, road_address, lot_address, district, region_group, region_quality,
    license_date, closure_date, status_code, status_name, status_class,
    detailed_status_code, detailed_status_name, room_count, room_count_quality,
    normalized_phone, longitude, latitude, projected_x, projected_y, coordinate_crs,
    source_updated_at, data_updated_on, data_update_point, source_payload_json, record_hash
) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_SQL = """
update staging_license_snapshot set
    jurisdiction_code = ?, source_name = ?, normalized_name = ?, road_address = ?, lot_address = ?,
    district = ?, region_group = ?, region_quality = ?, license_date = ?, closure_date = ?,
    status_code = ?, status_name = ?, status_class = ?, detailed_status_code = ?,
    detailed_status_name = ?, room_count = ?, room_count_quality = ?, normalized_phone = ?,
    longitude = ?, latitude = ?, projected_x = ?, projected_y = ?, coordinate_crs = ?,
    source_updated_at = ?, data_updated_on = ?, data_update_point = ?,
    source_payload_json = ?, record_hash = ?, last_loaded_run_id = ?
where source_id = ? and source_record_id = ? and observed_on = ?
"""


def load_license_snapshot(
    db: Database, records: Iterable[LicenseRecord], run_id: UUID
) -> int:
    """Insert or update Busan license observations, returning rows materially changed.

    Records outside Busan are deliberately filtered here, after normalization has retained
    their source payload.  Busan records with an unparsed district remain staged with an
    ``unresolved`` region quality for later review.
    """
    changed = 0
    for record in records:
        if not record.is_busan or record.source_record_id is None:
            continue
        values, record_hash = _payload(record)
        existing = db.query(
            """
            select record_hash from staging_license_snapshot
            where source_id = ? and source_record_id = ? and observed_on = ?
            """,
            [record.source_id, record.source_record_id, record.observed_on],
        )
        if not existing:
            db.connection.execute(
                _INSERT_SQL, [*values[:3], run_id, run_id, *values[3:], record_hash]
            )
            changed += 1
        elif existing[0][0] != record_hash:
            db.connection.execute(
                _UPDATE_SQL,
                [
                    *values[3:],
                    record_hash,
                    run_id,
                    record.source_id,
                    record.source_record_id,
                    record.observed_on,
                ],
            )
            changed += 1
        else:
            db.connection.execute(
                """
                update staging_license_snapshot set last_loaded_run_id = ?
                where source_id = ? and source_record_id = ? and observed_on = ?
                """,
                [run_id, record.source_id, record.source_record_id, record.observed_on],
            )
    return changed

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from westbusan.db import Database
from westbusan.spatial.enrich import enrich_current_facilities
from westbusan.spatial.geocode import GeocodeResult


class FixedGeocoder:
    def __init__(self, *, district: str = "북구") -> None:
        self.district = district
        self.used = False

    def resolve(self, address: str, *, address_type: str = "ROAD") -> GeocodeResult:
        if self.used:
            raise AssertionError("a cached address was sent to the provider again")
        self.used = True
        return GeocodeResult(
            status="matched",
            longitude=129.01025,
            latitude=35.20610,
            crs="EPSG:4326",
            district=self.district,
            response_hash="a" * 64,
        )


class NeverCallGeocoder:
    def resolve(self, address: str, *, address_type: str = "ROAD") -> GeocodeResult:
        raise AssertionError("cached enrichment must not call the provider")


def _database(tmp_path: Path) -> tuple[Database, UUID, UUID]:
    db = Database(tmp_path / "enrichment.duckdb", Path("sql"))
    db.migrate()
    run_id = uuid4()
    facility_id = uuid4()
    now = datetime.now(UTC)
    db.connection.execute(
        """insert into pipeline_run (
               run_id, mode, started_at, status, business_date, rebuildable
           ) values (?, 'fixture', ?, 'PUBLISHED', '2026-08-21', true)""",
        [run_id, now],
    )
    db.connection.execute(
        "insert into publication_state (publication_key, published_run_id) values ('current', ?)",
        [run_id],
    )
    db.connection.execute(
        """insert into dim_facility (
               facility_id, canonical_name, district, region_group
           ) values (?, '구포 시험숙박', '북구', 'west')""",
        [facility_id],
    )
    db.connection.execute(
        """insert into run_facility (
               run_id, facility_id, canonical_name, district, region_group
           ) values (?, ?, '구포 시험숙박', '북구', 'west')""",
        [run_id, facility_id],
    )
    return db, run_id, facility_id


def _license(
    db: Database,
    run_id: UUID,
    facility_id: UUID,
    *,
    road_address: str | None = "부산광역시 북구 시험로 1",
    lot_address: str | None = None,
) -> None:
    version_run_id = uuid4()
    source_id = "lodgings"
    record_id = str(uuid4())
    observed_on = date(2026, 8, 21)
    db.connection.execute(
        """insert into staging_license_revision (
               version_run_id, source_id, source_record_id, observed_on,
               revision_sequence, source_name, normalized_name, road_address,
               lot_address, district, region_group, region_quality,
               room_count_quality, source_payload_json, record_hash
           ) values (?, ?, ?, ?, 1, '구포 시험숙박', '구포 시험숙박', ?, ?,
                     '북구', 'west', 'reported', 'missing', '{}', ?)""",
        [
            version_run_id,
            source_id,
            record_id,
            observed_on,
            road_address,
            lot_address,
            f"hash-{record_id}",
        ],
    )
    db.connection.execute(
        """insert into run_facility_license (
               run_id, facility_id, source_id, source_record_id, evidence_json,
               selected_version_run_id, selected_observed_on,
               selected_revision_sequence
           ) values (?, ?, ?, ?, '{}', ?, ?, 1)""",
        [run_id, facility_id, source_id, record_id, version_run_id, observed_on],
    )


def test_enrichment_resolves_one_address_once_then_reuses_cache(tmp_path: Path) -> None:
    """Catches daily runs repeating calls for an unchanged current facility."""
    db, run_id, facility_id = _database(tmp_path)
    _license(db, run_id, facility_id)

    first = enrich_current_facilities(db, FixedGeocoder())
    second = enrich_current_facilities(db, NeverCallGeocoder())

    assert first.total == 1
    assert first.matched == 1
    assert first.cache_hits == 0
    assert second.total == 1
    assert second.matched == 1
    assert second.cache_hits == 1
    assert db.query(
        """select provider_status, provider_district, longitude, latitude,
                  address_kind
           from spatial_facility_location
           where base_published_run_id=? and facility_id=?""",
        [run_id, facility_id],
    ) == [("matched", "북구", 129.01025, 35.20610, "road")]


def test_enrichment_uses_parcel_address_when_road_address_is_absent(
    tmp_path: Path,
) -> None:
    """Catches valid lot-address-only facilities being dropped from the map."""
    db, run_id, facility_id = _database(tmp_path)
    _license(
        db,
        run_id,
        facility_id,
        road_address=None,
        lot_address="부산광역시 북구 시험동 1-1",
    )

    summary = enrich_current_facilities(db, FixedGeocoder())

    assert summary.matched == 1
    assert db.query(
        "select address_kind from spatial_facility_location where facility_id=?",
        [facility_id],
    ) == [("parcel",)]


def test_enrichment_rejects_provider_district_mismatch(tmp_path: Path) -> None:
    """Catches a valid Busan point being attached to the wrong district facility."""
    db, run_id, facility_id = _database(tmp_path)
    _license(db, run_id, facility_id)

    summary = enrich_current_facilities(db, FixedGeocoder(district="해운대구"))

    assert summary.district_mismatch == 1
    assert summary.matched == 0
    assert db.query(
        """select provider_status, longitude, latitude
           from spatial_facility_location
           where base_published_run_id=? and facility_id=?""",
        [run_id, facility_id],
    ) == [("district_mismatch", None, None)]


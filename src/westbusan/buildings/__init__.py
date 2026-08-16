"""Official legal-dong reference import and parcel-targeted building enrichment."""

from westbusan.buildings.load import (
    BuildingCollectionResult,
    ParcelQuery,
    collect_buildings_for_licenses,
    load_legal_dong_codes,
    parcel_query,
)
from westbusan.buildings.normalize import (
    BuildingRecord,
    building_age,
    normalize_building_title,
)

__all__ = [
    "BuildingCollectionResult",
    "BuildingRecord",
    "ParcelQuery",
    "building_age",
    "collect_buildings_for_licenses",
    "load_legal_dong_codes",
    "normalize_building_title",
    "parcel_query",
]

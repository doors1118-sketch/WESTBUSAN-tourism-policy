"""Private vacant-house source ingestion interfaces."""

from westbusan.vacant_house.cadastral import (
    CadastralFetch,
    VWorldCadastralClient,
)
from westbusan.vacant_house.correction import (
    CorrectedArchive,
    build_corrected_archive,
)
from westbusan.vacant_house.hub_models import (
    CadastralParcel,
    HubCandidate,
    VacantHub,
    VacantParcel,
)
from westbusan.vacant_house.hub_publish import (
    HubBuildInput,
    HubPublication,
    HubPublicationError,
    publish_hubs,
)
from westbusan.vacant_house.hubs import build_contiguous_hubs
from westbusan.vacant_house.models import (
    ArchiveProfile,
    VacantHouseSourceError,
    VacantHouseSourceRow,
)
from westbusan.vacant_house.parcel import build_pnu, collapse_to_parcels
from westbusan.vacant_house.source import iter_archive_rows, profile_archive

__all__ = [
    "ArchiveProfile",
    "CadastralFetch",
    "CadastralParcel",
    "CorrectedArchive",
    "HubBuildInput",
    "HubCandidate",
    "HubPublication",
    "HubPublicationError",
    "VWorldCadastralClient",
    "VacantHouseSourceError",
    "VacantHouseSourceRow",
    "VacantHub",
    "VacantParcel",
    "build_contiguous_hubs",
    "build_corrected_archive",
    "build_pnu",
    "collapse_to_parcels",
    "iter_archive_rows",
    "profile_archive",
    "publish_hubs",
]

"""Conservative normalization helpers for matching accommodation records."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizedAddress:
    """An address retained verbatim enough for review, with parsed locality hints."""

    value: str | None
    district: str | None
    is_busan: bool


def normalize_name(value: str | None) -> str | None:
    """Return a stable matching key without replacing the source business name."""
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    result = "".join(character for character in normalized if character.isalnum())
    return result or None


def normalize_phone(value: str | None) -> str | None:
    """Return phone digits only; presentation formatting remains in the raw payload."""
    if value is None:
        return None
    result = "".join(character for character in value if character.isdigit())
    return result or None


def normalize_address(value: str | None) -> NormalizedAddress:
    """Collapse whitespace and parse a Busan district without guessing one."""
    if value is None:
        return NormalizedAddress(value=None, district=None, is_busan=False)
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        return NormalizedAddress(value=None, district=None, is_busan=False)
    is_busan = bool(
        re.search(r"(?:^|\s)부산(?:광역시|시)?(?=\s|$)", normalized)
    )
    match = re.search(r"([가-힣]+(?:구|군))", normalized)
    district = match.group(1) if is_busan and match else None
    return NormalizedAddress(value=normalized, district=district, is_busan=is_busan)

"""Canonical spatial policy identity shared by preparation and mart stages."""

from __future__ import annotations

import hashlib
import json

from westbusan.config import Settings


def spatial_policy_version(settings: Settings) -> str:
    """Return the canonical policy hash pinned into every spatial run."""
    payload = {
        "policy": settings.policy.model_dump(mode="json"),
        "spatial": settings.spatial.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

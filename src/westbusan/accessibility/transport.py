"""Pure aggregation of source-native public-transport OD observations."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

_MONTH = re.compile(r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])$")


@dataclass(frozen=True, slots=True)
class TransportObservation:
    """One monthly origin-to-destination public-transport observation."""

    period: str
    origin_district_code: str
    origin_district_name: str
    origin_dong_code: str
    origin_dong_name: str
    destination_district_code: str
    destination_district_name: str
    destination_dong_code: str
    destination_dong_name: str
    value: float
    unit: str


@dataclass(frozen=True, slots=True)
class DongTransportMetric:
    """Monthly movement evidence for one destination dong."""

    period: str
    destination_district_code: str
    destination_district_name: str
    destination_dong_code: str
    destination_dong_name: str
    inbound_from_other_dong: float
    inbound_from_other_district: float
    outbound_to_other_dong: float
    net_inbound: float
    observation_count: int
    unit: str = "passengers"


@dataclass(slots=True)
class _Accumulator:
    district_code: str
    district_name: str
    dong_code: str
    dong_name: str
    inbound_other_dong: float = 0.0
    inbound_other_district: float = 0.0
    outbound_other_dong: float = 0.0
    observation_count: int = 0


def aggregate_dong_transport(
    rows: Iterable[TransportObservation],
) -> tuple[DongTransportMetric, ...]:
    """Aggregate monthly OD rows without interpreting trips as unique people."""
    accumulators: dict[tuple[str, str], _Accumulator] = {}
    for row in rows:
        _validate(row)
        origin_key = (row.period, row.origin_dong_code)
        destination_key = (row.period, row.destination_dong_code)
        origin = accumulators.setdefault(
            origin_key,
            _Accumulator(
                row.origin_district_code,
                row.origin_district_name,
                row.origin_dong_code,
                row.origin_dong_name,
            ),
        )
        destination = accumulators.setdefault(
            destination_key,
            _Accumulator(
                row.destination_district_code,
                row.destination_district_name,
                row.destination_dong_code,
                row.destination_dong_name,
            ),
        )
        involved = {origin_key, destination_key}
        for key in involved:
            accumulators[key].observation_count += 1
        if origin_key == destination_key:
            continue
        destination.inbound_other_dong += row.value
        origin.outbound_other_dong += row.value
        if row.origin_district_code != row.destination_district_code:
            destination.inbound_other_district += row.value

    metrics = [
        DongTransportMetric(
            period=period,
            destination_district_code=item.district_code,
            destination_district_name=item.district_name,
            destination_dong_code=item.dong_code,
            destination_dong_name=item.dong_name,
            inbound_from_other_dong=item.inbound_other_dong,
            inbound_from_other_district=item.inbound_other_district,
            outbound_to_other_dong=item.outbound_other_dong,
            net_inbound=item.inbound_other_dong - item.outbound_other_dong,
            observation_count=item.observation_count,
        )
        for (period, _), item in accumulators.items()
    ]
    return tuple(
        sorted(
            metrics,
            key=lambda item: (
                item.period,
                item.destination_district_code,
                item.destination_dong_code,
            ),
        )
    )


def _validate(row: TransportObservation) -> None:
    if row.unit != "passengers":
        raise ValueError("transport OD unit must be passengers")
    if not math.isfinite(row.value) or row.value < 0:
        raise ValueError("transport OD value must be finite and non-negative")
    if not _MONTH.fullmatch(row.period):
        raise ValueError("transport OD period must be YYYY-MM")
    required = (
        row.origin_district_code,
        row.origin_district_name,
        row.origin_dong_code,
        row.origin_dong_name,
        row.destination_district_code,
        row.destination_district_name,
        row.destination_dong_code,
        row.destination_dong_name,
    )
    if any(not value.strip() for value in required):
        raise ValueError("transport OD places must be non-empty")


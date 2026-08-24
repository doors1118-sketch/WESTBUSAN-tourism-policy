from __future__ import annotations

import pytest

from westbusan.accessibility.transport import (
    TransportObservation,
    aggregate_dong_transport,
)


def _observation(
    *,
    origin_district_code: str,
    origin_district_name: str,
    origin_dong_code: str,
    origin_dong_name: str,
    destination_district_code: str,
    destination_district_name: str,
    destination_dong_code: str,
    destination_dong_name: str,
    passengers: float,
    period: str = "2026-06",
    unit: str = "passengers",
) -> TransportObservation:
    return TransportObservation(
        period=period,
        origin_district_code=origin_district_code,
        origin_district_name=origin_district_name,
        origin_dong_code=origin_dong_code,
        origin_dong_name=origin_dong_name,
        destination_district_code=destination_district_code,
        destination_district_name=destination_district_name,
        destination_dong_code=destination_dong_code,
        destination_dong_name=destination_dong_name,
        value=passengers,
        unit=unit,
    )


def _sample_rows(*, unit: str = "passengers") -> tuple[TransportObservation, ...]:
    return (
        _observation(
            origin_district_code="26230",
            origin_district_name="부산진구",
            origin_dong_code="2623010100",
            origin_dong_name="부전동",
            destination_district_code="26320",
            destination_district_name="북구",
            destination_dong_code="2632010500",
            destination_dong_name="구포동",
            passengers=90,
            unit=unit,
        ),
        _observation(
            origin_district_code="26320",
            origin_district_name="북구",
            origin_dong_code="2632010400",
            origin_dong_name="덕천동",
            destination_district_code="26320",
            destination_district_name="북구",
            destination_dong_code="2632010500",
            destination_dong_name="구포동",
            passengers=60,
            unit=unit,
        ),
        _observation(
            origin_district_code="26320",
            origin_district_name="북구",
            origin_dong_code="2632010500",
            origin_dong_name="구포동",
            destination_district_code="26320",
            destination_district_name="북구",
            destination_dong_code="2632010400",
            destination_dong_name="덕천동",
            passengers=40,
            unit=unit,
        ),
        _observation(
            origin_district_code="26320",
            origin_district_name="북구",
            origin_dong_code="2632010500",
            origin_dong_name="구포동",
            destination_district_code="26320",
            destination_district_name="북구",
            destination_dong_code="2632010500",
            destination_dong_name="구포동",
            passengers=25,
            unit=unit,
        ),
    )


def test_aggregate_dong_transport_separates_other_dong_and_other_district() -> None:
    metrics = aggregate_dong_transport(_sample_rows())

    gu_po = next(
        item for item in metrics if item.destination_dong_name == "구포동"
    )

    assert gu_po.inbound_from_other_dong == 150
    assert gu_po.inbound_from_other_district == 90
    assert gu_po.outbound_to_other_dong == 40
    assert gu_po.net_inbound == 110
    assert gu_po.observation_count == 4
    assert gu_po.unit == "passengers"


def test_aggregate_dong_transport_keeps_months_separate() -> None:
    rows = _sample_rows() + (
        _observation(
            origin_district_code="26230",
            origin_district_name="부산진구",
            origin_dong_code="2623010100",
            origin_dong_name="부전동",
            destination_district_code="26320",
            destination_district_name="북구",
            destination_dong_code="2632010500",
            destination_dong_name="구포동",
            passengers=30,
            period="2026-07",
        ),
    )

    metrics = aggregate_dong_transport(rows)

    assert [item.period for item in metrics if item.destination_dong_name == "구포동"] == [
        "2026-06",
        "2026-07",
    ]
    july = next(
        item
        for item in metrics
        if item.period == "2026-07" and item.destination_dong_name == "구포동"
    )
    assert july.inbound_from_other_dong == 30
    assert july.inbound_from_other_district == 30


def test_aggregate_dong_transport_rejects_non_passenger_unit() -> None:
    with pytest.raises(ValueError, match="passengers"):
        aggregate_dong_transport(_sample_rows(unit="vehicles"))


def test_aggregate_dong_transport_rejects_negative_volume() -> None:
    rows = (
        _observation(
            origin_district_code="26230",
            origin_district_name="부산진구",
            origin_dong_code="2623010100",
            origin_dong_name="부전동",
            destination_district_code="26320",
            destination_district_name="북구",
            destination_dong_code="2632010500",
            destination_dong_name="구포동",
            passengers=-1,
        ),
    )

    with pytest.raises(ValueError, match="non-negative"):
        aggregate_dong_transport(rows)

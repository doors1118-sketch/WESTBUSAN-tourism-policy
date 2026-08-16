-- Rebuilt, run-scoped analytical marts.  Source-native facts are retained in
-- fact tables; this schema stores only explicitly compatible derived metrics.
create table if not exists mart_facility_current (
    run_id uuid not null,
    facility_id uuid not null,
    district varchar not null,
    region_group varchar not null,
    legal_registration_count integer not null,
    room_count double,
    room_count_quality varchar not null,
    has_tourism_registration boolean not null,
    has_foreigner_city_homestay boolean not null,
    has_foreign_visitor_capable_registration boolean not null,
    building_age_years double,
    building_age_quality varchar not null,
    recent_permit_event boolean,
    active boolean not null,
    primary key (run_id, facility_id)
);

create table if not exists mart_region_month (
    run_id uuid not null,
    district varchar not null,
    region_group varchar not null,
    period varchar not null,
    physical_facility_count integer not null,
    legal_registration_count integer not null,
    room_sum double,
    room_mean double,
    room_median double,
    room_q1 double,
    room_q3 double,
    room_known_facility_count integer not null,
    room_coverage double,
    small_facility_count integer,
    small_facility_share double,
    tourism_registration_facility_share double,
    tourism_registration_room_share double,
    foreigner_city_homestay_registration_share double,
    foreign_visitor_capable_registration_share double,
    building_age_mean double,
    building_age_median double,
    building_age_room_weighted_mean double,
    building_20y_share double,
    building_30y_share double,
    recent_five_year_permit_event_share double,
    active_openings integer not null,
    active_closures integer not null,
    active_net_change integer not null,
    visitors_per_100_rooms double,
    lodging_consumption_per_room double,
    transport_inflow_per_room double,
    visitor_growth_minus_room_supply_growth double,
    demand_pressure_band varchar not null,
    room_supply_band varchar not null,
    metric_evidence_json varchar not null,
    primary key (run_id, district, period)
);

create table if not exists mart_metric_evidence (
    run_id uuid not null,
    district varchar not null,
    region_group varchar not null,
    period varchar not null,
    metric_name varchar not null,
    metric_source_identity varchar not null,
    numerator double,
    denominator double,
    coverage double,
    source_period varchar not null,
    quality_band varchar not null,
    evidence_json varchar not null,
    primary key (run_id, district, period, metric_name)
);

create table if not exists mart_region_comparison (
    run_id uuid not null,
    period varchar not null,
    metric_name varchar not null,
    comparison_type varchar not null,
    value double,
    numerator double,
    denominator double,
    coverage double,
    quality_band varchar not null,
    evidence_json varchar not null,
    primary key (run_id, period, metric_name, comparison_type)
);

create table if not exists mart_policy_signal (
    run_id uuid not null,
    region_group varchar not null,
    period varchar not null,
    code varchar not null,
    evidence_json varchar not null,
    primary key (run_id, region_group, period, code)
);

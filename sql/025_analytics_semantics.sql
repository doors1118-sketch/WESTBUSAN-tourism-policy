create table if not exists bridge_facility_designation (
    facility_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    evidence_json varchar not null,
    linked_at timestamp with time zone not null default current_timestamp,
    primary key (facility_id, source_id, source_record_id)
);

create table if not exists entity_pair_adjudication (
    left_registration_key varchar not null,
    right_registration_key varchar not null,
    decision varchar not null check (decision in ('merge', 'separate')),
    reviewer varchar not null,
    rationale varchar not null,
    algorithm_version varchar not null,
    data_version varchar not null,
    created_at timestamp with time zone not null default current_timestamp,
    primary key (
        left_registration_key, right_registration_key, algorithm_version, data_version
    )
);

create table if not exists building_link_review (
    review_id uuid primary key,
    source_id varchar not null,
    source_record_id varchar not null,
    parcel_hash varchar not null,
    candidate_building_ids_json varchar not null,
    review_status varchar not null default 'pending',
    evidence_json varchar not null,
    created_at timestamp with time zone not null default current_timestamp
);

alter table mart_facility_current
    add column if not exists has_tourist_pension_designation boolean default false;

alter table mart_region_month
    alter column physical_facility_count drop not null;

alter table mart_region_month
    alter column legal_registration_count drop not null;

create table if not exists mart_region_group_month (
    run_id uuid not null,
    region_group varchar not null,
    period varchar not null,
    district_count integer not null,
    observed_district_count integer not null,
    physical_facility_count integer,
    legal_registration_count integer,
    room_sum double,
    room_known_facility_count integer not null,
    room_coverage double,
    age_known_facility_count integer not null,
    age_known_coverage double,
    evidence_json varchar not null,
    primary key (run_id, region_group, period)
);

alter table mart_policy_signal
    add column if not exists evaluation_status varchar default 'unavailable';

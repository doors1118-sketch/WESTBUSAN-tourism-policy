alter table mart_region_month
    rename column visitors_per_100_rooms to visitor_person_days_per_100_rooms;

create table if not exists facility_identity_anchor (
    facility_id uuid primary key,
    anchor_registration_key varchar not null unique,
    established_run_id uuid not null,
    established_at timestamp with time zone not null default current_timestamp
);

create table if not exists facility_identity_alias (
    alias_facility_id uuid primary key,
    canonical_facility_id uuid not null,
    reason varchar not null,
    created_run_id uuid not null,
    created_at timestamp with time zone not null default current_timestamp
);

create table if not exists facility_component_history (
    run_id uuid not null,
    facility_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    source_snapshot_run_id uuid,
    component_signature varchar not null,
    district varchar,
    region_group varchar,
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (run_id, source_id, source_record_id)
);

create table if not exists facility_designation_history (
    run_id uuid not null,
    facility_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    source_snapshot_run_id uuid,
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (run_id, source_id, source_record_id)
);

alter table building_link_review add column if not exists candidate_version varchar;
alter table building_link_review add column if not exists adjudicated_candidate_version varchar;
alter table building_link_review add column if not exists selected_building_id uuid;
alter table building_link_review add column if not exists reviewer varchar;
alter table building_link_review add column if not exists rationale varchar;

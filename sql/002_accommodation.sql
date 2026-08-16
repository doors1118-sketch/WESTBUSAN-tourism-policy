create table if not exists staging_license_snapshot (
    source_id varchar not null,
    source_record_id varchar not null,
    observed_on date not null,
    first_loaded_run_id uuid not null,
    last_loaded_run_id uuid not null,
    source_name varchar,
    normalized_name varchar,
    road_address varchar,
    lot_address varchar,
    district varchar,
    region_group varchar,
    region_quality varchar not null,
    license_date date,
    closure_date date,
    status_code varchar,
    status_name varchar,
    room_count integer,
    room_count_quality varchar not null,
    normalized_phone varchar,
    longitude double,
    latitude double,
    source_updated_at varchar,
    source_payload_json varchar not null,
    record_hash varchar not null,
    primary key (source_id, source_record_id, observed_on)
);

create table if not exists dim_facility (
    facility_id uuid primary key,
    canonical_name varchar,
    district varchar,
    region_group varchar,
    created_at timestamp not null default current_timestamp
);

create table if not exists bridge_facility_license (
    facility_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    linked_at timestamp not null default current_timestamp,
    primary key (facility_id, source_id, source_record_id)
);

create table if not exists dim_building (
    building_id uuid primary key,
    building_key varchar unique,
    road_address varchar,
    lot_address varchar,
    created_at timestamp not null default current_timestamp
);

create table if not exists bridge_facility_building (
    facility_id uuid not null,
    building_id uuid not null,
    linked_at timestamp not null default current_timestamp,
    primary key (facility_id, building_id)
);

create table if not exists fact_building_event (
    event_id uuid primary key,
    building_id uuid,
    event_type varchar not null,
    event_date date,
    source_payload_json varchar not null
);

create table if not exists duplicate_review (
    review_id uuid primary key,
    left_facility_id uuid,
    right_facility_id uuid,
    review_status varchar not null default 'pending',
    evidence_json varchar not null
);

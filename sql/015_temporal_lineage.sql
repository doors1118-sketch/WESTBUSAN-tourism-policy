create table if not exists pipeline_run_input (
    run_id uuid not null,
    input_run_id uuid not null,
    observed_at timestamp with time zone not null default current_timestamp,
    primary key (run_id, input_run_id)
);

create table if not exists staging_license_snapshot_version (
    version_run_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    observed_on date not null,
    jurisdiction_code varchar,
    source_name varchar,
    normalized_name varchar,
    road_address varchar,
    lot_address varchar,
    district varchar,
    region_group varchar,
    region_quality varchar not null,
    license_date date,
    license_date_quality varchar,
    closure_date date,
    closure_date_quality varchar,
    status_code varchar,
    status_name varchar,
    status_class varchar,
    detailed_status_code varchar,
    detailed_status_name varchar,
    room_count integer,
    room_count_quality varchar not null,
    normalized_phone varchar,
    longitude double,
    latitude double,
    projected_x double,
    projected_y double,
    coordinate_crs varchar,
    source_updated_at varchar,
    source_modified_on date,
    source_modified_date_quality varchar,
    data_updated_on date,
    data_updated_date_quality varchar,
    data_update_point varchar,
    source_payload_json varchar not null,
    record_hash varchar not null,
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (version_run_id, source_id, source_record_id, observed_on)
);

create table if not exists run_facility (
    run_id uuid not null,
    facility_id uuid not null,
    canonical_name varchar,
    district varchar,
    region_group varchar,
    primary key (run_id, facility_id)
);

create table if not exists run_facility_license (
    run_id uuid not null,
    facility_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    evidence_json varchar not null,
    primary key (run_id, facility_id, source_id, source_record_id)
);

create table if not exists run_facility_building (
    run_id uuid not null,
    facility_id uuid not null,
    building_id uuid not null,
    primary key (run_id, facility_id, building_id)
);

create table if not exists run_duplicate_review (
    run_id uuid not null,
    review_id uuid not null,
    left_facility_id uuid,
    right_facility_id uuid,
    review_status varchar not null default 'pending',
    evidence_json varchar not null,
    primary key (run_id, review_id)
);

create table if not exists staging_building_snapshot_version (
    version_run_id uuid not null,
    building_id varchar not null,
    observed_on date not null,
    parcel_hash varchar not null,
    sigungu_cd varchar,
    bjdong_cd varchar,
    plat_gb_cd varchar,
    bun varchar,
    ji varchar,
    road_address varchar,
    lot_address varchar,
    approval_date date,
    use_approval_date date,
    permit_date date,
    main_use varchar,
    total_area double,
    ground_floor_count integer,
    underground_floor_count integer,
    closed_indicator varchar,
    is_closed boolean not null,
    source_payload_json varchar not null,
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (version_run_id, building_id, observed_on)
);

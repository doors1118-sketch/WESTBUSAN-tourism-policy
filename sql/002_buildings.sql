create table if not exists reference_legal_dong (
    full_code varchar primary key,
    sigungu_cd varchar not null,
    bjdong_cd varchar not null,
    full_name varchar not null,
    active boolean not null
);

create table if not exists staging_building_snapshot (
    building_id varchar not null,
    observed_on date not null,
    first_loaded_run_id uuid not null,
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
    primary key (building_id, observed_on)
);

create table if not exists bridge_license_building (
    source_id varchar not null,
    source_record_id varchar not null,
    building_id uuid not null,
    parcel_hash varchar not null,
    linked_at timestamp not null default current_timestamp,
    primary key (source_id, source_record_id, building_id)
);

create table if not exists staging_license_revision (
    version_run_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    observed_on date not null,
    revision_sequence bigint not null,
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
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (
        version_run_id, source_id, source_record_id, observed_on, revision_sequence
    ),
    unique (version_run_id, source_id, source_record_id, observed_on, record_hash)
);

insert into staging_license_revision (
    version_run_id, source_id, source_record_id, observed_on, revision_sequence,
    source_name, normalized_name, road_address, lot_address, district,
    region_group, region_quality, license_date, closure_date, status_code,
    status_name, room_count, room_count_quality, normalized_phone, longitude,
    latitude, source_updated_at, source_payload_json, record_hash, recorded_at
)
select version_run_id, source_id, source_record_id, observed_on, 1,
       source_name, normalized_name, road_address, lot_address, district,
       region_group, region_quality, license_date, closure_date, status_code,
       status_name, room_count, room_count_quality, normalized_phone, longitude,
       latitude, source_updated_at, source_payload_json, record_hash, recorded_at
from staging_license_snapshot_version
on conflict do nothing;

create table if not exists staging_building_revision (
    version_run_id uuid not null,
    building_id varchar not null,
    observed_on date not null,
    revision_sequence bigint not null,
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
    record_hash varchar not null,
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (version_run_id, building_id, observed_on, revision_sequence),
    unique (version_run_id, building_id, observed_on, record_hash)
);

insert into staging_building_revision (
    version_run_id, building_id, observed_on, revision_sequence, parcel_hash,
    sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, road_address, lot_address,
    approval_date, use_approval_date, permit_date, main_use, total_area,
    ground_floor_count, underground_floor_count, closed_indicator, is_closed,
    source_payload_json, record_hash, recorded_at
)
select version_run_id, building_id, observed_on, 1, parcel_hash,
       sigungu_cd, bjdong_cd, plat_gb_cd, bun, ji, road_address, lot_address,
       approval_date, use_approval_date, permit_date, main_use, total_area,
       ground_floor_count, underground_floor_count, closed_indicator, is_closed,
       source_payload_json,
       sha256(concat_ws('|', building_id, observed_on::varchar, source_payload_json)),
       recorded_at
from staging_building_snapshot_version
on conflict do nothing;

create table if not exists run_license_building_observation (
    run_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    building_id uuid not null,
    parcel_hash varchar not null,
    observed_at timestamp with time zone not null default current_timestamp,
    primary key (run_id, source_id, source_record_id, building_id)
);

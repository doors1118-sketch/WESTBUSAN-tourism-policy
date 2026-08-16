create table if not exists staging_license_revision (
    version_run_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    observed_on date not null,
    revision_sequence bigint not null,
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
    primary key (
        version_run_id, source_id, source_record_id, observed_on, revision_sequence
    ),
    unique (version_run_id, source_id, source_record_id, observed_on, record_hash)
);

insert into staging_license_revision (
    version_run_id, source_id, source_record_id, observed_on, revision_sequence,
    jurisdiction_code, source_name, normalized_name, road_address, lot_address,
    district, region_group, region_quality, license_date, license_date_quality,
    closure_date, closure_date_quality, status_code, status_name, status_class,
    detailed_status_code, detailed_status_name, room_count, room_count_quality,
    normalized_phone, longitude, latitude, projected_x, projected_y, coordinate_crs,
    source_updated_at, source_modified_on, source_modified_date_quality,
    data_updated_on, data_updated_date_quality, data_update_point,
    source_payload_json, record_hash, recorded_at
)
select version_run_id, source_id, source_record_id, observed_on, 1,
       jurisdiction_code, source_name, normalized_name, road_address, lot_address,
       district, region_group, region_quality, license_date, license_date_quality,
       closure_date, closure_date_quality, status_code, status_name, status_class,
       detailed_status_code, detailed_status_name, room_count, room_count_quality,
       normalized_phone, longitude, latitude, projected_x, projected_y, coordinate_crs,
       source_updated_at, source_modified_on, source_modified_date_quality,
       data_updated_on, data_updated_date_quality, data_update_point,
       source_payload_json, record_hash, recorded_at
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

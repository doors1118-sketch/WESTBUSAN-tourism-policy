create table accessibility_snapshot (
    snapshot_id uuid primary key,
    core_run_id uuid not null,
    spatial_run_id uuid not null,
    business_date date not null,
    status varchar not null check (status in ('RUNNING', 'COMPLETED', 'FAILED')),
    transport_status varchar not null,
    tourism_status varchar not null,
    transport_observation_count bigint not null default 0,
    transport_dong_month_count bigint not null default 0,
    tourism_poi_count bigint not null default 0,
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone,
    unique (core_run_id, spatial_run_id, business_date)
);

create table mart_transport_dong_month (
    snapshot_id uuid not null,
    period varchar not null,
    destination_district_code varchar not null,
    destination_district_name varchar not null,
    destination_dong_code varchar not null,
    destination_dong_name varchar not null,
    inbound_other_dong double not null,
    inbound_other_district double not null,
    outbound_other_dong double not null,
    net_inbound double not null,
    observation_count bigint not null,
    unit varchar not null check (unit = 'passengers'),
    source_id varchar not null,
    source_period varchar not null,
    evidence_json json not null,
    primary key (snapshot_id, period, destination_dong_code)
);

create table dim_tourism_poi_snapshot (
    snapshot_id uuid not null,
    content_id varchar not null,
    title varchar not null,
    category_code varchar,
    category_name varchar,
    district_code varchar,
    district_name varchar,
    dong_code varchar,
    dong_name varchar,
    longitude double not null,
    latitude double not null,
    source_id varchar not null,
    source_period varchar not null,
    evidence_json json not null,
    primary key (snapshot_id, content_id)
);

create table mart_grid_accessibility (
    snapshot_id uuid not null,
    grid_id varchar not null,
    transport_period varchar,
    transport_inbound double,
    nearest_transport_hub_name varchar,
    nearest_transport_hub_distance_m double,
    tourism_poi_count_1000m bigint,
    nearest_tourism_poi_name varchar,
    nearest_tourism_poi_distance_m double,
    transport_coverage_status varchar not null,
    tourism_coverage_status varchar not null,
    evidence_json json not null,
    primary key (snapshot_id, grid_id)
);

create table mart_vacant_candidate_accessibility (
    snapshot_id uuid not null,
    candidate_id varchar not null,
    district_name varchar not null,
    dong_name varchar,
    transport_period varchar,
    transport_inbound double,
    nearest_transport_hub_name varchar,
    nearest_transport_hub_distance_m double,
    tourism_poi_count_1000m bigint,
    nearest_tourism_poi_name varchar,
    nearest_tourism_poi_distance_m double,
    transport_score double,
    tourism_score double,
    ranking_eligible boolean not null default false,
    coverage_status varchar not null,
    evidence_json json not null,
    primary key (snapshot_id, candidate_id)
);

create table accessibility_completion_manifest (
    snapshot_id uuid primary key,
    core_run_id uuid not null,
    spatial_run_id uuid not null,
    business_date date not null,
    transport_row_count bigint not null,
    tourism_poi_count bigint not null,
    grid_row_count bigint not null,
    vacant_candidate_row_count bigint not null,
    manifest_hash varchar not null,
    completed_at timestamp with time zone not null
);

create table accessibility_publication_current (
    publication_key varchar primary key check (publication_key = 'current'),
    snapshot_id uuid not null,
    business_date date not null,
    published_at timestamp with time zone not null
);

create table if not exists fact_tourism_demand (
    source_id varchar not null,
    metric_code varchar not null,
    period varchar not null,
    district varchar not null,
    region_group varchar not null,
    dimension_json varchar not null,
    dimension_json_hash varchar not null,
    source_revision varchar not null,
    metric_value double not null,
    unit varchar not null,
    source_payload_json varchar not null,
    artifact_id uuid not null,
    loaded_run_id uuid not null,
    loaded_at timestamp with time zone not null default current_timestamp,
    unique (source_id, metric_code, period, district, dimension_json_hash, source_revision)
);

create table if not exists fact_transport_flow (
    source_id varchar not null,
    metric_code varchar not null,
    period varchar not null,
    district varchar not null,
    region_group varchar not null,
    dimension_json varchar not null,
    dimension_json_hash varchar not null,
    source_revision varchar not null,
    metric_value double not null,
    unit varchar not null,
    source_payload_json varchar not null,
    artifact_id uuid not null,
    loaded_run_id uuid not null,
    loaded_at timestamp with time zone not null default current_timestamp,
    unique (source_id, metric_code, period, district, dimension_json_hash, source_revision)
);

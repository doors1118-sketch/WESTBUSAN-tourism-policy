create table vacant_house_parcel_context_run (
    context_run_id uuid primary key,
    inventory_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    status varchar not null check (status in ('RUNNING', 'FAILED', 'COMPLETED')),
    source_contract_json varchar not null check (json_valid(source_contract_json)),
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone,
    failure_evidence_json varchar check (
        failure_evidence_json is null or json_valid(failure_evidence_json)
    ),
    unique (context_run_id, inventory_run_id)
);

create table vacant_house_parcel_context_response (
    context_run_id uuid not null,
    inventory_run_id uuid not null,
    pnu varchar not null check (regexp_full_match(pnu, '[0-9]{19}')),
    source_id varchar not null,
    dataset varchar not null,
    provider_status varchar not null check (
        provider_status in ('matched', 'not_found', 'provider_error', 'invalid_response')
    ),
    source_date date,
    request_identity varchar not null check (json_valid(request_identity)),
    response_sha256 varchar not null check (length(response_sha256) = 64),
    evidence_json varchar not null check (json_valid(evidence_json)),
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (context_run_id, pnu, source_id),
    foreign key (context_run_id, inventory_run_id)
        references vacant_house_parcel_context_run(context_run_id, inventory_run_id)
);

create table vacant_house_parcel_context_observation (
    context_run_id uuid not null,
    inventory_run_id uuid not null,
    record_id uuid not null,
    pnu varchar not null check (regexp_full_match(pnu, '[0-9]{19}')),
    source_id varchar not null,
    dataset varchar not null,
    provider_status varchar not null check (
        provider_status in ('matched', 'not_found', 'provider_error', 'invalid_response')
    ),
    land_use_zone varchar,
    land_use_district varchar,
    land_use_area varchar,
    land_category varchar,
    parcel_area double check (parcel_area is null or parcel_area >= 0),
    road_side varchar,
    terrain_height varchar,
    terrain_shape varchar,
    land_use_situation varchar,
    source_date date,
    request_identity varchar not null check (json_valid(request_identity)),
    response_sha256 varchar not null check (length(response_sha256) = 64),
    evidence_json varchar not null check (json_valid(evidence_json)),
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (context_run_id, record_id, source_id),
    foreign key (context_run_id, inventory_run_id)
        references vacant_house_parcel_context_run(context_run_id, inventory_run_id),
    foreign key (inventory_run_id, record_id)
        references vacant_house_current(vacant_run_id, record_id)
);

create table vacant_house_parcel_context_publication_current (
    singleton_key integer primary key default 1 check (singleton_key = 1),
    context_run_id uuid not null unique references vacant_house_parcel_context_run(context_run_id),
    inventory_run_id uuid not null,
    published_at timestamp with time zone not null,
    publisher varchar not null,
    publication_reason varchar not null,
    foreign key (context_run_id, inventory_run_id)
        references vacant_house_parcel_context_run(context_run_id, inventory_run_id)
);

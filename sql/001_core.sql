create table if not exists schema_migrations (
    version varchar primary key,
    applied_at timestamp with time zone not null default current_timestamp
);

create table if not exists pipeline_run (
    run_id uuid primary key,
    mode varchar not null,
    started_at timestamp with time zone not null,
    status varchar not null
);

create table if not exists raw_artifact (
    artifact_id uuid primary key,
    run_id uuid not null,
    source_id varchar not null,
    ingest_date date not null,
    request_json varchar not null,
    request_hash varchar not null,
    content_hash varchar not null,
    path varchar not null,
    created_at timestamp with time zone not null
);

create table if not exists source_status (
    source_id varchar not null,
    checked_at timestamp with time zone not null,
    status varchar not null,
    detail_json varchar not null,
    primary key (source_id, checked_at)
);

create table if not exists collection_checkpoint (
    source_id varchar not null,
    partition_key varchar not null,
    checkpoint_json varchar not null,
    updated_at timestamp with time zone not null,
    primary key (source_id, partition_key)
);

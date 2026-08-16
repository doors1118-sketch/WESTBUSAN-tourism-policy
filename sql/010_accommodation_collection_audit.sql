create table if not exists accommodation_collection_audit (
    run_id uuid not null,
    source_id varchar not null,
    artifact_id uuid not null,
    page_no integer not null,
    endpoint varchar not null,
    jurisdiction_parameter varchar not null,
    jurisdiction_expected varchar not null,
    accepted_count integer not null,
    out_of_scope_count integer not null,
    rejected_count integer not null,
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (run_id, source_id, artifact_id)
);

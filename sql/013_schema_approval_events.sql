create table if not exists quality_schema_approval_event (
    approval_event_id uuid primary key,
    source_id varchar not null,
    operation varchar not null,
    partition_key varchar not null,
    approved_schema_fingerprint varchar not null,
    approval_method varchar not null,
    approver varchar,
    rationale varchar,
    approved_at timestamp with time zone not null default current_timestamp
);

alter table quality_schema_baseline
    add column if not exists approval_event_id uuid;

alter table pipeline_run add column if not exists business_date date;

update pipeline_run
set business_date = cast(started_at at time zone 'Asia/Seoul' as date)
where business_date is null;

create table if not exists mart_build_manifest (
    run_id uuid primary key,
    manifest_hash varchar not null,
    table_counts_json varchar not null,
    completed_at timestamp with time zone not null default current_timestamp
);

create table if not exists publication_rollback_audit (
    audit_id uuid primary key,
    previous_run_id uuid not null,
    replacement_run_id uuid not null,
    reason varchar not null,
    recorded_at timestamp with time zone not null default current_timestamp
);

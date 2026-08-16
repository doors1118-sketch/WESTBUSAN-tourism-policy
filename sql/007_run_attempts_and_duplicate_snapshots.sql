alter table pipeline_run add column if not exists logical_run_key varchar;
alter table pipeline_run add column if not exists attempt integer;
alter table pipeline_run add column if not exists finished_at timestamp with time zone;
alter table pipeline_run add column if not exists created_at timestamp with time zone
    default current_timestamp;

create unique index if not exists pipeline_run_logical_attempt
    on pipeline_run (logical_run_key, attempt);

create table if not exists pipeline_run_summary (
    run_id uuid primary key,
    mode varchar not null,
    status varchar not null,
    published boolean not null,
    raw_artifacts integer not null,
    row_count integer not null,
    warning_count integer not null,
    failed_required_checks integer not null,
    started_at timestamp with time zone not null,
    finished_at timestamp with time zone not null
);

create table if not exists publication_duplicate_review_snapshot (
    run_id uuid not null,
    review_id uuid not null,
    left_facility_id uuid,
    right_facility_id uuid,
    review_status varchar not null,
    evidence_json varchar not null,
    captured_at timestamp with time zone not null default current_timestamp,
    primary key (run_id, review_id)
);

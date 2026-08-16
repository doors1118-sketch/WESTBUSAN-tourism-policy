alter table source_status add column if not exists run_id uuid;

alter table staging_license_snapshot add column if not exists last_loaded_run_id uuid;
update staging_license_snapshot
set last_loaded_run_id = first_loaded_run_id
where last_loaded_run_id is null;

create table if not exists quality_suite_manifest (
    run_id uuid primary key,
    report_hash varchar not null,
    expected_checks_json varchar not null,
    check_count integer not null,
    completed_at timestamp with time zone not null default current_timestamp
);

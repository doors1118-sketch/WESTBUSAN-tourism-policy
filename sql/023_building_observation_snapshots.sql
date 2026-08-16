create table if not exists run_license_building_snapshot (
    producer_run_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    completed_at timestamp with time zone not null default current_timestamp,
    primary key (producer_run_id, source_id, source_record_id)
);

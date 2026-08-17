create table if not exists spatial_run_summary (
    spatial_run_id uuid primary key,
    base_published_run_id uuid not null,
    boundary_version_id uuid not null,
    policy_version varchar not null,
    business_date date not null,
    table_counts_json varchar not null check (json_valid(table_counts_json)),
    table_digests_json varchar not null check (json_valid(table_digests_json)),
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone not null,
    published_at timestamp with time zone not null
);

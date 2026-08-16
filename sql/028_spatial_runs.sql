create table if not exists spatial_run (
    spatial_run_id uuid primary key,
    base_published_run_id uuid not null,
    boundary_version_id uuid not null,
    policy_version varchar not null,
    business_date date not null,
    status varchar not null,
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone,
    owner varchar,
    lease_expires_at timestamp with time zone,
    fence_epoch bigint not null check (fence_epoch >= 0),
    failure_evidence_json varchar check (
        failure_evidence_json is null or json_valid(failure_evidence_json)
    )
);

create table if not exists spatial_writer_lease (
    lease_key varchar primary key check (lease_key = 'writer'),
    spatial_run_id uuid,
    owner varchar,
    lease_expires_at timestamp with time zone,
    fence_epoch bigint not null check (fence_epoch >= 0)
);

insert into spatial_writer_lease (
    lease_key, spatial_run_id, owner, lease_expires_at, fence_epoch
) values ('writer', null, null, null, 0)
on conflict (lease_key) do nothing;

create table if not exists spatial_mart_completion_manifest (
    spatial_run_id uuid not null,
    table_name varchar not null,
    row_count bigint not null check (row_count >= 0),
    row_digest varchar not null,
    schema_version varchar not null,
    completed_at timestamp with time zone not null default current_timestamp,
    primary key (spatial_run_id, table_name)
);

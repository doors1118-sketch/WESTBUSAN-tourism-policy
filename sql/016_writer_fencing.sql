alter table pipeline_run add column if not exists writer_fence_epoch bigint;

create table if not exists pipeline_writer_lease (
    lease_key varchar primary key,
    owner_token uuid,
    run_id uuid,
    fence_epoch bigint not null,
    heartbeat_at timestamp with time zone not null,
    lease_expires_at timestamp with time zone not null,
    check (lease_key = 'writer')
);

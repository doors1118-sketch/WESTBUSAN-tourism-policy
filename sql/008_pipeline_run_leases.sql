alter table pipeline_run add column if not exists lease_owner_token uuid;
alter table pipeline_run add column if not exists lease_expires_at timestamp with time zone;
alter table pipeline_run add column if not exists heartbeat_at timestamp with time zone;

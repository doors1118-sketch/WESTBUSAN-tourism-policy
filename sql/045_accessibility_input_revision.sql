create table accessibility_snapshot_revised (
    snapshot_id uuid primary key,
    core_run_id uuid not null,
    spatial_run_id uuid not null,
    business_date date not null,
    status varchar not null check (status in ('RUNNING', 'COMPLETED', 'FAILED')),
    transport_status varchar not null,
    tourism_status varchar not null,
    transport_observation_count bigint not null default 0,
    transport_dong_month_count bigint not null default 0,
    tourism_poi_count bigint not null default 0,
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone
);

insert into accessibility_snapshot_revised
select * from accessibility_snapshot;

drop table accessibility_snapshot;

alter table accessibility_snapshot_revised rename to accessibility_snapshot;

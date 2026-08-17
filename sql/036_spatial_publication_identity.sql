create table spatial_run_summary_036 (
    spatial_run_id uuid primary key,
    base_published_run_id uuid not null,
    boundary_version_id uuid not null,
    policy_version varchar not null,
    business_date date not null,
    table_counts_json varchar not null check (json_valid(table_counts_json)),
    table_digests_json varchar not null check (json_valid(table_digests_json)),
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone not null,
    published_at timestamp with time zone not null,
    publication_event_id uuid not null,
    publisher varchar not null,
    previous_spatial_run_id uuid,
    publication_action varchar not null,
    publication_reason varchar not null
);

insert into spatial_run_summary_036
select summary.spatial_run_id,
       summary.base_published_run_id,
       summary.boundary_version_id,
       summary.policy_version,
       summary.business_date,
       summary.table_counts_json,
       summary.table_digests_json,
       summary.started_at,
       summary.completed_at,
       summary.published_at,
       audit.event_id,
       audit.actor,
       audit.old_spatial_run_id,
       audit.action,
       audit.reason
from spatial_run_summary as summary
join spatial_publication_audit as audit
  on audit.spatial_run_id = summary.spatial_run_id
 and audit.base_published_run_id = summary.base_published_run_id
 and audit.new_spatial_run_id = summary.spatial_run_id
 and audit.business_date = summary.business_date
 and audit.event_at = summary.published_at;

create temporary table spatial_run_summary_036_guard (
    identities_complete boolean not null check (identities_complete)
);

insert into spatial_run_summary_036_guard
select (select count(*) from spatial_run_summary_036)
     = (select count(*) from spatial_run_summary);

drop table spatial_run_summary;
alter table spatial_run_summary_036 rename to spatial_run_summary;
drop table spatial_run_summary_036_guard;

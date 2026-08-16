select case when exists (
    select 1
    from mart_spatial_exception as exception
    left join spatial_run as spatial
      on spatial.spatial_run_id = exception.spatial_run_id
    where spatial.spatial_run_id is null
) then error('spatial exception has no matching spatial run') end;

create table mart_spatial_exception_with_lineage (
    spatial_run_id uuid not null,
    base_published_run_id uuid not null,
    subject_type varchar not null,
    subject_id varchar not null,
    exception_code varchar not null,
    redacted_evidence_json varchar not null check (json_valid(redacted_evidence_json)),
    resolution_status varchar not null,
    primary key (spatial_run_id, subject_type, subject_id, exception_code)
);

insert into mart_spatial_exception_with_lineage (
    spatial_run_id, base_published_run_id, subject_type, subject_id, exception_code,
    redacted_evidence_json, resolution_status
)
select exception.spatial_run_id, spatial.base_published_run_id,
       exception.subject_type, exception.subject_id, exception.exception_code,
       exception.redacted_evidence_json, exception.resolution_status
from mart_spatial_exception as exception
join spatial_run as spatial
  on spatial.spatial_run_id = exception.spatial_run_id;

drop table mart_spatial_exception;
alter table mart_spatial_exception_with_lineage rename to mart_spatial_exception;

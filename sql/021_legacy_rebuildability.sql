alter table pipeline_run add column if not exists rebuildable boolean default true;

update pipeline_run as run
set rebuildable = false
where not exists (
    select 1 from pipeline_run_input as lineage
    where lineage.run_id = run.run_id and lineage.input_run_id = run.run_id
);

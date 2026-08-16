-- Every run that existed when this upgrade is applied must be re-audited.
update pipeline_run set rebuildable = false;

create table if not exists legacy_migration_audit (
    audit_id uuid primary key,
    run_id uuid not null,
    operator_identity varchar not null,
    reason varchar not null,
    audited_at timestamp with time zone not null default current_timestamp,
    evidence_json varchar not null,
    decision varchar not null
);

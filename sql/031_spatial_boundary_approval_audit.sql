create table if not exists spatial_boundary_approval_event (
    event_id uuid primary key,
    observed_content_hash varchar not null,
    boundary_version_id uuid,
    action varchar not null check (action in ('approved', 'rejected')),
    actor varchar not null,
    rationale varchar not null,
    source_metadata_json varchar not null check (json_valid(source_metadata_json)),
    evidence_json varchar not null check (json_valid(evidence_json)),
    event_at timestamp with time zone not null default current_timestamp
);

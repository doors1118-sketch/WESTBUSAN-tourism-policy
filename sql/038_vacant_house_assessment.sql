create table vacant_house_assessment_run (
    assessment_run_id uuid primary key,
    inventory_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    base_published_run_id uuid not null references pipeline_run(run_id),
    spatial_run_id uuid not null references spatial_run_summary(spatial_run_id),
    boundary_version_id uuid not null references spatial_boundary_version(boundary_version_id),
    policy_version varchar not null,
    status varchar not null check (status in ('RUNNING', 'FAILED', 'COMPLETED')),
    owner_token uuid,
    fence_epoch bigint not null check (fence_epoch >= 0),
    lease_expires_at timestamp with time zone,
    inventory_current_count bigint not null default 0 check (inventory_current_count >= 0),
    enrichment_count bigint not null default 0 check (enrichment_count >= 0),
    screening_count bigint not null default 0 check (screening_count >= 0),
    blocking_exception_count bigint not null default 0
        check (blocking_exception_count >= 0),
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone,
    failure_evidence_json varchar check (
        failure_evidence_json is null or json_valid(failure_evidence_json)
    ),
    unique (assessment_run_id, inventory_run_id)
);

create table vacant_house_enrichment (
    assessment_run_id uuid not null,
    inventory_run_id uuid not null,
    record_id uuid not null,
    building_id varchar,
    building_match_quality varchar,
    wgs84_longitude double,
    wgs84_latitude double,
    projected_x double,
    projected_y double,
    grid_id varchar,
    geometry_match_json varchar check (
        geometry_match_json is null or json_valid(geometry_match_json)
    ),
    land_use_match_json varchar check (
        land_use_match_json is null or json_valid(land_use_match_json)
    ),
    source_dates_json varchar not null default '{}' check (json_valid(source_dates_json)),
    coverage double check (coverage is null or (coverage >= 0 and coverage <= 1)),
    evidence_json varchar not null check (json_valid(evidence_json)),
    primary key (assessment_run_id, record_id),
    unique (assessment_run_id, inventory_run_id, record_id),
    foreign key (assessment_run_id, inventory_run_id)
        references vacant_house_assessment_run(assessment_run_id, inventory_run_id),
    foreign key (inventory_run_id, record_id)
        references vacant_house_current(vacant_run_id, record_id)
);

create table vacant_house_screening (
    assessment_run_id uuid not null,
    record_id uuid not null,
    evidence_completeness varchar not null default 'unknown',
    feasibility_class varchar not null check (feasibility_class in (
        'priority_review', 'conditional_review', 'deprioritise', 'insufficient_evidence'
    )),
    opportunity_components_json varchar not null default '{}' check (
        json_valid(opportunity_components_json)
    ),
    opportunity_band varchar not null,
    exclusion_reason_codes_json varchar not null default '[]' check (
        json_valid(exclusion_reason_codes_json)
    ),
    conditional_reason_codes_json varchar not null default '[]' check (
        json_valid(conditional_reason_codes_json)
    ),
    missing_evidence_codes_json varchar not null default '[]' check (
        json_valid(missing_evidence_codes_json)
    ),
    policy_version varchar not null,
    assessed_at timestamp with time zone not null default current_timestamp,
    evidence_json varchar not null check (json_valid(evidence_json)),
    primary key (assessment_run_id, record_id),
    foreign key (assessment_run_id, record_id)
        references vacant_house_enrichment(assessment_run_id, record_id)
);

create table vacant_house_assessment_exception (
    exception_id uuid primary key,
    assessment_run_id uuid not null,
    inventory_run_id uuid not null,
    record_id uuid,
    exception_code varchar not null,
    safe_message varchar not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    resolution_status varchar not null check (resolution_status in ('OPEN', 'RESOLVED', 'WAIVED')),
    created_at timestamp with time zone not null,
    resolved_at timestamp with time zone,
    foreign key (assessment_run_id, inventory_run_id)
        references vacant_house_assessment_run(assessment_run_id, inventory_run_id),
    foreign key (inventory_run_id, record_id)
        references vacant_house_current(vacant_run_id, record_id)
);

create table vacant_house_assessment_manifest (
    manifest_id uuid primary key,
    assessment_run_id uuid not null references vacant_house_assessment_run(assessment_run_id),
    table_name varchar not null,
    row_count bigint not null check (row_count >= 0),
    row_digest_sha256 varchar not null check (length(row_digest_sha256) = 64),
    schema_version varchar not null,
    manifest_json varchar not null check (json_valid(manifest_json)),
    created_at timestamp with time zone not null,
    unique (assessment_run_id, table_name),
    unique (assessment_run_id, manifest_id)
);

create table vacant_house_assessment_publication_current (
    singleton_key integer primary key default 1 check (singleton_key = 1),
    pointer_id uuid not null unique,
    assessment_run_id uuid not null unique references vacant_house_assessment_run(assessment_run_id),
    published_at timestamp with time zone not null,
    publisher varchar not null,
    publication_event_id uuid not null,
    manifest_id uuid not null,
    foreign key (assessment_run_id, manifest_id)
        references vacant_house_assessment_manifest(assessment_run_id, manifest_id)
);

create table vacant_house_assessment_publication_audit (
    event_id uuid primary key,
    assessment_run_id uuid not null references vacant_house_assessment_run(assessment_run_id),
    old_assessment_run_id uuid references vacant_house_assessment_run(assessment_run_id),
    new_assessment_run_id uuid not null references vacant_house_assessment_run(assessment_run_id),
    action varchar not null,
    actor varchar not null,
    reason varchar not null,
    manifest_id uuid not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    event_at timestamp with time zone not null,
    foreign key (new_assessment_run_id, manifest_id)
        references vacant_house_assessment_manifest(assessment_run_id, manifest_id),
    unique (assessment_run_id, new_assessment_run_id, event_at)
);

create table vacant_house_detail_access_audit (
    access_id uuid primary key,
    assessment_run_id uuid not null,
    record_id uuid not null,
    user_subject varchar not null,
    purpose varchar not null,
    access_kind varchar not null,
    record_set_digest varchar not null check (length(record_set_digest) = 64),
    output_digest varchar check (output_digest is null or length(output_digest) = 64),
    accessed_at timestamp with time zone not null,
    foreign key (assessment_run_id, record_id)
        references vacant_house_enrichment(assessment_run_id, record_id)
);

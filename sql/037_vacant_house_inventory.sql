create table vacant_house_import_run (
    vacant_run_id uuid primary key,
    source_snapshot_date date not null,
    archive_sha256 varchar not null check (length(archive_sha256) = 64),
    bundle_manifest_sha256 varchar not null check (length(bundle_manifest_sha256) = 64),
    schema_version varchar not null,
    status varchar not null check (status in ('RUNNING','FAILED','COMPLETED')),
    owner_token uuid,
    fence_epoch bigint not null,
    lease_expires_at timestamp with time zone,
    source_row_count bigint not null default 0,
    accepted_record_count bigint not null default 0,
    exception_count bigint not null default 0,
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone,
    failure_evidence_json varchar check (
        failure_evidence_json is null or json_valid(failure_evidence_json)
    )
);

create table vacant_house_source_artifact (
    artifact_id uuid primary key,
    vacant_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    artifact_kind varchar not null,
    archive_sha256 varchar not null check (length(archive_sha256) = 64),
    workbook_sha256 varchar not null check (length(workbook_sha256) = 64),
    workbook_name varchar not null,
    sheet_name varchar not null,
    source_district varchar,
    observed_header_version varchar,
    source_row_count bigint not null default 0,
    conversion_provenance_json varchar not null check (json_valid(conversion_provenance_json)),
    created_at timestamp with time zone not null,
    unique (vacant_run_id, artifact_id)
);

create table vacant_house_revision (
    vacant_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    source_row_id varchar not null,
    record_id uuid not null,
    district_code varchar,
    district_name varchar,
    legal_dong_code varchar,
    legal_dong_name varchar,
    lot_type varchar,
    main_lot varchar,
    sub_lot varchar,
    road_code varchar,
    building_main varchar,
    building_sub varchar,
    building_name varchar,
    dong_name varchar,
    unit_name varchar,
    road_address varchar,
    exact_address varchar,
    housing_type varchar,
    construction_year integer,
    building_area double,
    land_area double,
    vacant_grade integer,
    original_grade_text varchar,
    cleanup_status varchar,
    source_artifact_id uuid not null,
    source_workbook_name varchar not null,
    source_sheet_name varchar not null,
    source_row_number bigint not null,
    record_hash varchar not null check (length(record_hash) = 64),
    duplicate_group_id varchar,
    review_status varchar,
    evidence_quality varchar,
    source_flags_json varchar check (source_flags_json is null or json_valid(source_flags_json)),
    primary key (vacant_run_id, source_row_id),
    unique (vacant_run_id, record_id, source_row_id),
    foreign key (vacant_run_id, source_artifact_id)
        references vacant_house_source_artifact(vacant_run_id, artifact_id)
);

create table vacant_house_current (
    vacant_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    record_id uuid not null,
    selected_source_row_id varchar not null,
    selected_at timestamp with time zone not null,
    primary key (vacant_run_id, record_id),
    foreign key (vacant_run_id, record_id, selected_source_row_id)
        references vacant_house_revision(vacant_run_id, record_id, source_row_id)
);

create table vacant_house_exception (
    exception_id uuid primary key,
    vacant_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    source_artifact_id uuid references vacant_house_source_artifact(artifact_id),
    source_row_id varchar,
    exception_code varchar not null,
    safe_message varchar not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    resolution_status varchar not null check (resolution_status in ('OPEN','RESOLVED','WAIVED')),
    created_at timestamp with time zone not null,
    resolved_at timestamp with time zone
);

create table vacant_house_completion_manifest (
    manifest_id uuid primary key,
    vacant_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    table_name varchar not null,
    row_count bigint not null check (row_count >= 0),
    row_digest_sha256 varchar not null check (length(row_digest_sha256) = 64),
    schema_version varchar not null,
    manifest_json varchar not null check (json_valid(manifest_json)),
    created_at timestamp with time zone not null,
    unique (vacant_run_id, table_name),
    unique (vacant_run_id, manifest_id)
);

create table vacant_house_publication_current (
    singleton_key integer primary key default 1 check (singleton_key = 1),
    pointer_id uuid not null unique,
    vacant_run_id uuid not null unique references vacant_house_import_run(vacant_run_id),
    published_at timestamp with time zone not null,
    publisher varchar not null,
    publication_event_id uuid not null,
    manifest_id uuid not null,
    foreign key (vacant_run_id, manifest_id)
        references vacant_house_completion_manifest(vacant_run_id, manifest_id)
);

create table vacant_house_publication_audit (
    event_id uuid primary key,
    vacant_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    old_vacant_run_id uuid references vacant_house_import_run(vacant_run_id),
    new_vacant_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    action varchar not null,
    actor varchar not null,
    reason varchar not null,
    manifest_id uuid not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    event_at timestamp with time zone not null,
    foreign key (new_vacant_run_id, manifest_id)
        references vacant_house_completion_manifest(vacant_run_id, manifest_id),
    unique (vacant_run_id, new_vacant_run_id, event_at)
);

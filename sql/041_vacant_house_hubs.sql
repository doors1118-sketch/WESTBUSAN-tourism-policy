create table vacant_house_hub_run (
    hub_run_id uuid primary key,
    inventory_run_id uuid not null references vacant_house_import_run(vacant_run_id),
    assessment_run_id uuid,
    policy_version varchar not null,
    status varchar not null check (status in ('RUNNING', 'FAILED', 'COMPLETED')),
    owner_token uuid,
    fence_epoch bigint not null check (fence_epoch >= 0),
    lease_expires_at timestamp with time zone,
    inventory_parcel_count bigint not null default 0
        check (inventory_parcel_count >= 0),
    evidence_count bigint not null default 0 check (evidence_count >= 0),
    matched_geometry_count bigint not null default 0
        check (matched_geometry_count >= 0),
    eligible_hub_count bigint not null default 0 check (eligible_hub_count >= 0),
    candidate_count bigint not null default 0
        check (candidate_count >= 0 and candidate_count <= 10),
    started_at timestamp with time zone not null,
    completed_at timestamp with time zone,
    failure_evidence_json varchar check (
        failure_evidence_json is null or json_valid(failure_evidence_json)
    ),
    unique (hub_run_id, inventory_run_id),
    foreign key (assessment_run_id, inventory_run_id)
        references vacant_house_assessment_run(assessment_run_id, inventory_run_id)
);

create table vacant_house_cadastral_evidence (
    hub_run_id uuid not null,
    inventory_run_id uuid not null,
    pnu varchar not null check (length(pnu) = 19),
    district_code varchar not null check (length(district_code) = 5),
    legal_dong_code varchar not null check (length(legal_dong_code) = 5),
    request_identity_json varchar not null check (json_valid(request_identity_json)),
    response_sha256 varchar not null check (length(response_sha256) = 64),
    raw_response_json varchar not null check (json_valid(raw_response_json)),
    provider_status varchar not null check (
        provider_status in (
            'matched', 'not_found', 'provider_error', 'invalid_response'
        )
    ),
    geometry_wkb blob,
    geometry_hash varchar check (
        geometry_hash is null or length(geometry_hash) = 64
    ),
    source_date date,
    retry_count integer not null default 0 check (retry_count >= 0),
    observed_at timestamp with time zone not null,
    primary key (hub_run_id, pnu),
    unique (hub_run_id, inventory_run_id, pnu),
    check (
        (
            provider_status = 'matched'
            and geometry_wkb is not null
            and geometry_hash is not null
        )
        or
        (
            provider_status <> 'matched'
            and geometry_wkb is null
            and geometry_hash is null
        )
    )
);

create table vacant_house_hub (
    hub_run_id uuid not null,
    inventory_run_id uuid not null,
    hub_id varchar not null,
    component_id varchar not null,
    candidate_rank integer check (candidate_rank between 1 and 10),
    parcel_count integer not null check (parcel_count >= 3),
    union_area double not null check (union_area > 0),
    geometry_wkb blob not null,
    geometry_hash varchar not null check (length(geometry_hash) = 64),
    district_codes_json varchar not null check (json_valid(district_codes_json)),
    legal_dong_codes_json varchar not null check (json_valid(legal_dong_codes_json)),
    context_json varchar not null default '{}' check (json_valid(context_json)),
    reason_codes_json varchar not null default '[]' check (json_valid(reason_codes_json)),
    primary key (hub_run_id, hub_id),
    unique (hub_run_id, inventory_run_id, hub_id),
    unique (hub_run_id, component_id),
    unique (hub_run_id, candidate_rank)
);

create table vacant_house_hub_member (
    hub_run_id uuid not null,
    inventory_run_id uuid not null,
    hub_id varchar not null,
    pnu varchar not null,
    member_order integer not null check (member_order >= 1),
    source_record_count integer not null check (source_record_count >= 1),
    primary key (hub_run_id, hub_id, pnu),
    unique (hub_run_id, hub_id, member_order),
    foreign key (hub_run_id, inventory_run_id, hub_id)
        references vacant_house_hub(hub_run_id, inventory_run_id, hub_id),
    foreign key (hub_run_id, inventory_run_id, pnu)
        references vacant_house_cadastral_evidence(
            hub_run_id, inventory_run_id, pnu
        )
);

create table vacant_house_hub_manifest (
    manifest_id uuid primary key,
    hub_run_id uuid not null,
    table_name varchar not null,
    row_count bigint not null check (row_count >= 0),
    row_digest_sha256 varchar not null check (length(row_digest_sha256) = 64),
    schema_version varchar not null,
    manifest_json varchar not null check (json_valid(manifest_json)),
    created_at timestamp with time zone not null,
    unique (hub_run_id, table_name),
    unique (hub_run_id, manifest_id)
);

create table vacant_house_hub_publication_current (
    singleton_key integer primary key default 1 check (singleton_key = 1),
    pointer_id uuid not null unique,
    hub_run_id uuid not null unique references vacant_house_hub_run(hub_run_id),
    published_at timestamp with time zone not null,
    publisher varchar not null,
    publication_event_id uuid not null,
    manifest_id uuid not null,
    foreign key (hub_run_id, manifest_id)
        references vacant_house_hub_manifest(hub_run_id, manifest_id)
);

create table vacant_house_hub_publication_audit (
    event_id uuid primary key,
    hub_run_id uuid not null references vacant_house_hub_run(hub_run_id),
    old_hub_run_id uuid references vacant_house_hub_run(hub_run_id),
    new_hub_run_id uuid not null references vacant_house_hub_run(hub_run_id),
    action varchar not null,
    actor varchar not null,
    reason varchar not null,
    manifest_id uuid not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    event_at timestamp with time zone not null,
    check (hub_run_id = new_hub_run_id),
    foreign key (new_hub_run_id, manifest_id)
        references vacant_house_hub_manifest(hub_run_id, manifest_id),
    unique (hub_run_id, new_hub_run_id, event_at)
);

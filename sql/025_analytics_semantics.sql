create table if not exists bridge_facility_designation (
    facility_id uuid not null,
    source_id varchar not null,
    source_record_id varchar not null,
    evidence_json varchar not null,
    linked_at timestamp with time zone not null default current_timestamp,
    primary key (facility_id, source_id, source_record_id)
);

create table if not exists entity_pair_adjudication (
    left_registration_key varchar not null,
    right_registration_key varchar not null,
    decision varchar not null check (decision in ('merge', 'separate')),
    reviewer varchar not null,
    rationale varchar not null,
    algorithm_version varchar not null,
    data_version varchar not null,
    created_at timestamp with time zone not null default current_timestamp,
    primary key (
        left_registration_key, right_registration_key, algorithm_version, data_version
    )
);

create table if not exists building_link_review (
    review_id uuid primary key,
    source_id varchar not null,
    source_record_id varchar not null,
    parcel_hash varchar not null,
    candidate_building_ids_json varchar not null,
    review_status varchar not null default 'pending',
    evidence_json varchar not null,
    created_at timestamp with time zone not null default current_timestamp
);

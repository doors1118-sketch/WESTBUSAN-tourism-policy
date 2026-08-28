create table if not exists nakdong_parcel_regulation_sync_run (
    run_id uuid primary key,
    checked_at timestamp with time zone not null,
    completed_at timestamp with time zone not null,
    parcel_count integer not null,
    designation_count integer not null,
    source_name varchar not null,
    source_url varchar not null,
    content_hash varchar not null,
    status varchar not null,
    check (parcel_count > 0),
    check (status = 'PUBLISHED')
);

create table if not exists nakdong_parcel_regulation_snapshot (
    run_id uuid not null,
    pnu varchar not null,
    land_use_status varchar not null,
    land_use_response_sha256 varchar not null,
    land_characteristics_status varchar not null,
    land_characteristics_response_sha256 varchar not null,
    land_characteristics_json varchar,
    source_date date,
    primary key (run_id, pnu),
    check (length(pnu) = 19),
    check (land_use_status = 'matched')
);

create table if not exists nakdong_parcel_designation_snapshot (
    run_id uuid not null,
    pnu varchar not null,
    designation_order integer not null,
    designation_name varchar not null,
    designation_category varchar not null,
    primary key (run_id, pnu, designation_order)
);

create table if not exists nakdong_parcel_regulation_publication_current (
    publication_key varchar primary key,
    run_id uuid not null,
    published_at timestamp with time zone not null,
    check (publication_key = 'current')
);

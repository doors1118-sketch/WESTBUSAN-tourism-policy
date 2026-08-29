create table if not exists nakdong_parcel_geometry_sync_run (
    run_id uuid primary key,
    checked_at timestamp with time zone not null,
    completed_at timestamp with time zone not null,
    target_count integer not null,
    matched_count integer not null,
    not_found_count integer not null,
    provider_error_count integer not null,
    invalid_response_count integer not null,
    source_name varchar not null,
    source_url varchar not null,
    crs varchar not null,
    content_hash varchar not null,
    status varchar not null,
    check (target_count > 0),
    check (matched_count >= 0 and matched_count <= target_count),
    check (
        matched_count + not_found_count + provider_error_count
        + invalid_response_count = target_count
    ),
    check (status = 'PUBLISHED')
);

create table if not exists nakdong_parcel_geometry_snapshot (
    run_id uuid not null,
    pnu varchar not null,
    provider_status varchar not null,
    request_identity varchar not null,
    response_sha256 varchar not null,
    geometry_wkb blob,
    geometry_sha256 varchar,
    minimum_longitude double,
    minimum_latitude double,
    maximum_longitude double,
    maximum_latitude double,
    source_date date,
    primary key (run_id, pnu),
    check (length(pnu) = 19),
    check (
        provider_status in (
            'matched', 'not_found', 'provider_error', 'invalid_response'
        )
    ),
    check (
        (provider_status = 'matched' and geometry_wkb is not null
         and geometry_sha256 is not null)
        or
        (provider_status <> 'matched' and geometry_wkb is null
         and geometry_sha256 is null)
    )
);

create table if not exists nakdong_parcel_geometry_publication_current (
    publication_key varchar primary key,
    run_id uuid not null,
    published_at timestamp with time zone not null,
    check (publication_key = 'current')
);

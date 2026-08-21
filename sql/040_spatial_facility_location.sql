alter table spatial_geocode_cache
    add column if not exists provider_district varchar;

create table spatial_facility_location (
    base_published_run_id uuid not null references pipeline_run(run_id),
    facility_id uuid not null references dim_facility(facility_id),
    address_hash varchar not null references spatial_geocode_cache(address_hash),
    address_kind varchar not null check (address_kind in ('road', 'parcel')),
    provider_status varchar not null check (
        provider_status in (
            'matched', 'not_found', 'provider_error',
            'invalid_response', 'district_mismatch'
        )
    ),
    provider_district varchar,
    longitude double,
    latitude double,
    evidence_json varchar not null check (json_valid(evidence_json)),
    observed_at timestamp with time zone not null,
    primary key (base_published_run_id, facility_id),
    check (
        (provider_status = 'matched' and longitude is not null and latitude is not null)
        or
        (provider_status <> 'matched' and longitude is null and latitude is null)
    )
);

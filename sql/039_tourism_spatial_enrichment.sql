create table spatial_geocode_cache (
    address_hash varchar primary key check (length(address_hash) = 64),
    normalized_address varchar not null check (length(trim(normalized_address)) > 0),
    longitude double,
    latitude double,
    provider_status varchar not null check (
        provider_status in (
            'matched', 'not_found', 'provider_error', 'invalid_response'
        )
    ),
    response_hash varchar check (
        response_hash is null or length(response_hash) = 64
    ),
    source_artifact_id uuid references raw_artifact(artifact_id),
    observed_at timestamp with time zone not null,
    check (
        (provider_status = 'matched' and longitude is not null and latitude is not null)
        or
        (provider_status <> 'matched' and longitude is null and latitude is null)
    )
);

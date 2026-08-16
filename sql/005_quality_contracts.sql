create table if not exists quality_source_contract (
    source_id varchar primary key,
    required_for_publication boolean not null,
    source_group varchar not null,
    cadence varchar not null
);

insert into quality_source_contract (source_id, required_for_publication, source_group, cadence)
values
    ('lodgings', true, 'accommodation', 'daily'),
    ('tourist_accommodations', true, 'accommodation', 'daily'),
    ('foreigner_city_homestays', true, 'accommodation', 'daily'),
    ('rural_homestays', true, 'accommodation', 'daily'),
    ('hanok_experience', true, 'accommodation', 'daily'),
    ('tourist_pensions', true, 'accommodation', 'daily'),
    ('building_register_title', false, 'building', 'monthly'),
    ('building_register_basis_outline', false, 'building', 'monthly'),
    ('building_permit_basis_outline', false, 'building', 'monthly'),
    ('building_permit_site', false, 'building', 'monthly'),
    ('closed_register_basis_outline', false, 'building', 'monthly'),
    ('tourism_data_lab', false, 'tourism', 'monthly'),
    ('area_tourism_demand', false, 'tourism', 'monthly'),
    ('area_tourism_consumption', false, 'tourism', 'monthly'),
    ('tourism_concentration_rate', false, 'tourism', 'monthly'),
    ('area_tourism_destination_division', false, 'tourism', 'monthly'),
    ('related_tourism_destinations', false, 'tourism', 'monthly'),
    ('public_transport_od_usage', false, 'transport', 'monthly'),
    ('busan_metro_odcloud_discovery', false, 'transport', 'monthly'),
    ('korail_workplace_ticketing_file', false, 'transport', 'monthly'),
    ('korail_residence_ticketing_file', false, 'transport', 'monthly'),
    ('srt_station_boarding_file', false, 'transport', 'monthly')
on conflict (source_id) do nothing;

create table if not exists quality_schema_baseline (
    source_id varchar not null,
    operation varchar not null,
    partition_key varchar not null default '*',
    approved_schema_fingerprint varchar not null,
    approval_method varchar not null,
    approved_at timestamp with time zone not null default current_timestamp,
    primary key (source_id, operation, partition_key)
);

create table if not exists staging_building_response (
    run_id uuid not null,
    source_id varchar not null,
    operation varchar not null,
    parcel_hash varchar not null,
    source_date date not null,
    page_no integer not null,
    total_count integer not null,
    row_count integer not null,
    schema_fingerprint varchar not null,
    artifact_id uuid not null,
    primary key (run_id, source_id, operation, parcel_hash, page_no)
);

alter table quality_suite_manifest add column if not exists contract_checks_json varchar;

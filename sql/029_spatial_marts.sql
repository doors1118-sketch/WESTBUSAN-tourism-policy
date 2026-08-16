create table if not exists mart_facility_priority_current (
    spatial_run_id uuid not null,
    base_published_run_id uuid not null,
    facility_id uuid not null,
    grid_id varchar not null,
    public_name varchar not null,
    public_address varchar,
    public_longitude double,
    public_latitude double,
    room_count double,
    use_approval_age_years double,
    district_code varchar,
    district_name varchar,
    small_scale_rating varchar not null,
    small_scale_points double not null,
    aged_building_rating varchar not null,
    aged_building_points double not null,
    district_context_rating varchar not null,
    district_context_points double not null,
    composite_score double not null,
    composite_grade varchar not null,
    display_status varchar not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    primary key (spatial_run_id, facility_id)
);

create table if not exists mart_grid_month (
    spatial_run_id uuid not null,
    base_published_run_id uuid not null,
    grid_id varchar not null,
    district_code varchar,
    district_name varchar,
    primary_dong_code varchar,
    primary_dong_name varchar,
    period varchar not null,
    physical_facility_count integer not null check (physical_facility_count >= 0),
    legal_registration_count integer not null check (legal_registration_count >= 0),
    room_sum double,
    room_coverage double check (room_coverage is null or (
        room_coverage >= 0 and room_coverage <= 1
    )),
    small_facility_count integer,
    small_facility_share double,
    age_sample_size integer not null check (age_sample_size >= 0),
    age_coverage double check (age_coverage is null or (
        age_coverage >= 0 and age_coverage <= 1
    )),
    age_20y_facility_count integer,
    age_20y_share double,
    age_30y_facility_count integer,
    age_30y_share double,
    coordinate_sample_size integer not null check (coordinate_sample_size >= 0),
    coordinate_coverage double check (coordinate_coverage is null or (
        coordinate_coverage >= 0 and coordinate_coverage <= 1
    )),
    district_context_rating varchar not null,
    district_context_points double not null,
    small_scale_rating varchar not null,
    small_scale_points double not null,
    aged_building_rating varchar not null,
    aged_building_points double not null,
    composite_score double not null,
    composite_grade varchar not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    primary key (spatial_run_id, grid_id, period)
);

create table if not exists mart_spatial_evidence (
    spatial_run_id uuid not null,
    base_published_run_id uuid not null,
    subject_type varchar not null,
    subject_id varchar not null,
    period varchar not null,
    metric_name varchar not null,
    source_identity varchar not null,
    source_period varchar not null,
    numerator double,
    denominator double,
    coverage double,
    quality_band varchar not null,
    evidence_json varchar not null check (json_valid(evidence_json)),
    primary key (spatial_run_id, subject_type, subject_id, period, metric_name)
);

create table if not exists mart_spatial_exception (
    spatial_run_id uuid not null,
    subject_type varchar not null,
    subject_id varchar not null,
    exception_code varchar not null,
    redacted_evidence_json varchar not null check (json_valid(redacted_evidence_json)),
    resolution_status varchar not null,
    primary key (spatial_run_id, subject_type, subject_id, exception_code)
);

create table if not exists spatial_publication_current (
    publication_key varchar primary key check (publication_key = 'current'),
    spatial_run_id uuid not null,
    business_date date not null,
    published_at timestamp with time zone not null
);

create table if not exists spatial_publication_audit (
    event_id uuid primary key,
    spatial_run_id uuid not null,
    base_published_run_id uuid not null,
    old_spatial_run_id uuid,
    new_spatial_run_id uuid,
    action varchar not null,
    actor varchar not null,
    reason varchar not null,
    business_date date not null,
    event_at timestamp with time zone not null default current_timestamp
);

create table if not exists spatial_boundary_version (
    boundary_version_id uuid primary key,
    raw_artifact_id uuid not null,
    content_hash varchar not null unique,
    source_organization varchar not null,
    source_url varchar not null,
    source_date date not null,
    source_version varchar not null,
    crs varchar not null,
    district_count integer not null check (district_count >= 0),
    dong_count integer not null check (dong_count >= 0),
    approved_by varchar not null,
    approval_rationale varchar not null,
    approved_at timestamp with time zone not null default current_timestamp
);

create table if not exists dim_spatial_grid_500m (
    boundary_version_id uuid not null,
    grid_id varchar not null,
    x_index integer not null,
    y_index integer not null,
    district_code varchar,
    district_name varchar,
    primary_dong_code varchar,
    primary_dong_name varchar,
    centroid_projected_x double not null,
    centroid_projected_y double not null,
    centroid_wgs84_longitude double not null,
    centroid_wgs84_latitude double not null,
    geometry_geojson varchar not null check (json_valid(geometry_geojson)),
    overlap_evidence_json varchar not null check (json_valid(overlap_evidence_json)),
    clipped_area_ratio double not null
        check (clipped_area_ratio >= 0 and clipped_area_ratio <= 1),
    primary key (boundary_version_id, grid_id)
);

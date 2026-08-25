create table building_investment_profile_observation (
    version_run_id uuid not null,
    building_id varchar not null,
    observed_on date not null,
    land_use_zone varchar,
    land_use_district varchar,
    land_use_area varchar,
    land_category varchar,
    site_area double check (site_area is null or site_area >= 0),
    building_area double check (building_area is null or building_area >= 0),
    total_area double check (total_area is null or total_area >= 0),
    building_coverage_ratio double check (
        building_coverage_ratio is null or building_coverage_ratio >= 0
    ),
    floor_area_ratio double check (floor_area_ratio is null or floor_area_ratio >= 0),
    main_use varchar,
    structure varchar,
    height double check (height is null or height >= 0),
    parking_total integer check (parking_total is null or parking_total >= 0),
    elevator_total integer check (elevator_total is null or elevator_total >= 0),
    earthquake_design_applied boolean,
    field_coverage double not null check (field_coverage between 0 and 1),
    source_payload_sha256 varchar not null check (length(source_payload_sha256) = 64),
    evidence_json varchar not null check (json_valid(evidence_json)),
    recorded_at timestamp with time zone not null default current_timestamp,
    primary key (version_run_id, building_id, observed_on)
);

create view building_investment_profile_latest as
select * exclude (profile_rank)
from (
    select profile.*, row_number() over (
        partition by building_id order by observed_on desc, recorded_at desc, version_run_id desc
    ) as profile_rank
    from building_investment_profile_observation as profile
)
where profile_rank = 1;

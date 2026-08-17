alter table mart_facility_priority_current
    alter column small_scale_points drop not null;
alter table mart_facility_priority_current
    alter column aged_building_points drop not null;
alter table mart_facility_priority_current
    alter column district_context_points drop not null;
alter table mart_facility_priority_current
    alter column composite_score drop not null;

alter table mart_grid_month
    alter column small_scale_points drop not null;
alter table mart_grid_month
    alter column aged_building_points drop not null;
alter table mart_grid_month
    alter column district_context_points drop not null;
alter table mart_grid_month
    alter column composite_score drop not null;

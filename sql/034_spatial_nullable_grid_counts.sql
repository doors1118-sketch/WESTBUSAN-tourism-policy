alter table mart_grid_month
    alter column physical_facility_count drop not null;
alter table mart_grid_month
    alter column legal_registration_count drop not null;
alter table mart_grid_month
    alter column age_sample_size drop not null;
alter table mart_grid_month
    alter column coordinate_sample_size drop not null;

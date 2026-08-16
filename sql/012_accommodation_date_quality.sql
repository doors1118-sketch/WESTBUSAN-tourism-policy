alter table staging_license_snapshot add column if not exists license_date_quality varchar;
alter table staging_license_snapshot add column if not exists closure_date_quality varchar;
alter table staging_license_snapshot add column if not exists source_modified_on date;
alter table staging_license_snapshot add column if not exists source_modified_date_quality varchar;
alter table staging_license_snapshot add column if not exists data_updated_date_quality varchar;

alter table run_facility_license
    add column if not exists selected_version_run_id uuid;
alter table run_facility_license
    add column if not exists selected_observed_on date;
alter table run_facility_license
    add column if not exists selected_revision_sequence bigint;

alter table bridge_facility_license
    add column if not exists evidence_json varchar;

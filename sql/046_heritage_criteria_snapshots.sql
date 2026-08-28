create table if not exists heritage_criteria_sync_run (
    run_id uuid primary key,
    checked_at timestamp with time zone not null,
    completed_at timestamp with time zone not null,
    bounds_json varchar not null,
    designation_count integer not null,
    criteria_zone_count integer not null,
    source_name varchar not null,
    source_url varchar not null,
    content_hash varchar not null,
    status varchar not null,
    check (status = 'PUBLISHED')
);

create table if not exists heritage_designation_zone_snapshot (
    run_id uuid not null,
    layer_name varchar not null,
    gid bigint not null,
    cp_cd varchar,
    heritage_name varchar,
    geometry_json varchar not null,
    primary key (run_id, layer_name, gid)
);

create table if not exists heritage_criteria_zone_snapshot (
    run_id uuid not null,
    layer_name varchar not null,
    gid bigint not null,
    pmpg_seid varchar not null,
    zone_code varchar,
    zone_name varchar not null,
    geometry_json varchar not null,
    criteria_json varchar not null,
    source_url varchar not null,
    primary key (run_id, layer_name, gid)
);

create table if not exists heritage_criteria_publication_current (
    publication_key varchar primary key,
    run_id uuid not null,
    published_at timestamp with time zone not null,
    check (publication_key = 'current')
);

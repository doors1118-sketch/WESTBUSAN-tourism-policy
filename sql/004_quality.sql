create table if not exists fact_data_quality (
    check_id uuid primary key,
    run_id uuid not null,
    check_name varchar not null,
    status varchar not null,
    actual_json varchar not null,
    expected_json varchar not null,
    severity varchar not null,
    source_id varchar,
    table_name varchar,
    evidence_json varchar not null,
    checked_at timestamp with time zone not null default current_timestamp
);

create table if not exists publication_state (
    publication_key varchar primary key,
    published_run_id uuid not null,
    published_at timestamp with time zone not null default current_timestamp,
    check (publication_key = 'current')
);

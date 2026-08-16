alter table fact_tourism_demand add column if not exists observation_key varchar;
alter table fact_transport_flow add column if not exists observation_key varchar;

create table if not exists run_fact_observation (
    run_id uuid not null,
    family varchar not null,
    observation_key varchar not null,
    observed_at timestamp with time zone not null default current_timestamp,
    primary key (run_id, family, observation_key)
);

create unique index if not exists uq_fact_tourism_observation_key
    on fact_tourism_demand (observation_key);
create unique index if not exists uq_fact_transport_observation_key
    on fact_transport_flow (observation_key);

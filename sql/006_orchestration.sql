alter table publication_state add column if not exists is_current boolean default true;

update publication_state
set is_current = publication_key = 'current';

alter table spatial_writer_lease
    add column if not exists fence_touch bigint default 0;

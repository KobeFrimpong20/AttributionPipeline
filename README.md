# AttributionPipeline

A small pipeline for learning ClickHouse real-time aggregation through marketing
attribution. A Python generator streams simulated e-commerce events into ClickHouse,
and materialized views keep aggregate tables up to date. See [PRD.md](PRD.md) for the
full spec.

## Run it

```sh
cp .env.example .env   # first run only
docker compose up --build
```

ClickHouse runs `clickhouse/init/01_schema.sql` the first time it starts with an empty
data volume. If you edit the schema, wipe the volume so the script runs again. This
deletes all stored data:

```sh
docker compose down -v
```

## Check that it works

Open a ClickHouse shell:

```sh
docker compose exec clickhouse clickhouse-client
```

Row counts should climb in all four tables:

```sql
SELECT 'events' AS table_name, count() AS row_count FROM attribution.events
UNION ALL SELECT 'identity_links', count() FROM attribution.identity_links
UNION ALL SELECT 'anon_touches', count() FROM attribution.anon_touches
UNION ALL SELECT 'funnel_minute', count() FROM attribution.funnel_minute;
```

The aggregate tables can have fewer rows than you might expect, and the counts can
drop, because background merges combine rows that share a sort key.

## Generator settings

Set these under `generator.environment` in `docker-compose.yml`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVENTS_PER_SEC` | 200 | Steady event rate |
| `BATCH_SIZE` | 10000 | Maximum rows per INSERT (`docker-compose.yml` sets 1000) |
| `FLUSH_INTERVAL_SEC` | 1 | Maximum time between INSERTs |
| `NUM_CUSTOMERS` | 0 | Customers to simulate before exiting; 0 runs until stopped |
| `SEED` | unset | Set for a reproducible run |

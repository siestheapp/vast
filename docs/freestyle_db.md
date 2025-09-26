## Freestyle local PostgreSQL database

This document captures how to connect to the locally restored `freestyle` PostgreSQL database and a few handy commands.

### Connection details
- Host: `127.0.0.1`
- Port: `5432`
- Database: `freestyle`
- User: `seandavey`
- Password: not set (local connection; leave blank)
- SSL mode: `disable` (or TablePlus “Preferred”)

Connection URI:

```
postgresql://seandavey@127.0.0.1:5432/freestyle?sslmode=disable
```

### Quick connect (psql)

```
psql -h 127.0.0.1 -p 5432 -U seandavey -d freestyle
```

If your local setup requires a password, set it temporarily:

```
PGPASSWORD="<your_password>" psql -h 127.0.0.1 -p 5432 -U seandavey -d freestyle
```

### TablePlus
Create a new PostgreSQL connection with:
- Name: freestyle
- Host: 127.0.0.1
- Port: 5432
- User: seandavey
- Password: leave blank
- Database: freestyle
- SSL mode: Preferred

### Notes on restore
- Restored from: `freestyle_20250923_232810.dump`
- Restore method: `pg_restore` from PostgreSQL 17 client into local server (PostgreSQL 14 via Homebrew)
- Some Supabase-specific extensions were not installed locally; restore completed with warnings but core schemas and data are present.

### Useful diagnostics

Top N largest tables in the current database:

```
SELECT
  relname AS table_name,
  pg_size_pretty(pg_total_relation_size(relid)) AS total,
  pg_size_pretty(pg_relation_size(relid)) AS table_size,
  pg_size_pretty(pg_total_relation_size(relid) - pg_relation_size(relid)) AS indexes
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 10;
```

Approx row counts by table (fast estimate):

```
SELECT
  schemaname,
  relname AS table_name,
  n_live_tup AS approx_rows
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC
LIMIT 20;
```

Recent activity snapshot:

```
SELECT
  now() AS at,
  tup_returned,
  tup_fetched,
  tup_inserted,
  tup_updated,
  tup_deleted
FROM pg_stat_database
WHERE datname = current_database();
```



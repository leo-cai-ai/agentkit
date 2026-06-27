-- Runs once on first database init (empty data dir) via the Postgres image's
-- /docker-entrypoint-initdb.d hook. Enables pgvector so the `memories` table
-- (auto-created lazily by PgVectorStore) can use the `vector` type.
CREATE EXTENSION IF NOT EXISTS vector;

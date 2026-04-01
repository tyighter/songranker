# SongRanker Scaffold

This scaffold includes:

- FastAPI backend with a minimal server-rendered frontend.
- PostgreSQL database.
- Alembic migrations for `users`, `songs`, `pairwise_votes`, and `rating_scores`.
- Health check endpoint at `/health`.

## Quick start

### 1) Start everything

```bash
docker compose up --build
```

The app will be available at:

- http://0.0.0.0:2112
- http://localhost:2112

### 2) Verify health

```bash
curl http://localhost:2112/health
```

Expected response:

```json
{"status":"ok"}
```

## Environment variables

The app reads the following DB environment variables:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

By default, `docker-compose.yml` wires these to the `db` service.

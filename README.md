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


## Rating API

- `GET /api/rate/next?user_id=<id>&artist=<optional>&title_query=<optional>&song_ids=<optional_csv>`
  - Returns the next deterministic comparison pair for the user, with optional active filters.
- `POST /api/rate/vote`
  - Body: `user_id`, `winner_song_id`, `loser_song_id`, and `filters` context.
  - Applies an Elo update to global per-user song ratings, records vote history, and stores rating snapshots for the vote.


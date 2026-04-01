# SongRanker Scaffold

This scaffold includes:

- FastAPI backend with server-rendered setup and ranking pages.
- SQLite database persisted on a Docker volume.
- Alembic migrations for users, songs, pairwise votes, rating scores, setup settings, and cached Plex metadata.
- Health check endpoint at `/health`.

## Quick start

### 1) Start everything

```bash
docker compose up --build
```

The app will be available at:

- http://0.0.0.0:2112
- http://localhost:2112

### 2) Migration behavior (container startup)

Migrations are already executed automatically at container startup by the `Dockerfile` command (`alembic upgrade head` before `uvicorn`).

If you are reusing an existing `/data/songranker.db` volume and the schema exists but Alembic history is missing, repair migration tracking first:

```bash
docker compose exec songranker alembic stamp head
docker compose exec songranker alembic upgrade head
```

For local development, if migration state is messy and you can safely reset data, use a destructive volume reset:

```bash
docker compose down -v
docker compose up --build
```

### 3) Open setup wizard

On first launch, SongRanker redirects all app traffic to `/setup` until initialization is complete.

Setup steps:

1. Create the first local user (username/email/password).
2. Save Plex URL and token.
3. Pick Plex music library section.
4. Run initial import to cache metadata locally.

Imported metadata includes `title`, `artist`, `album`, `year`, `decade`, and `plex_rating_key`.


## Lightweight auth (not secure yet)

- Username/password auth is intentionally lightweight and **not secure yet**.
- Session cookie auth uses a server-side `user_sessions` table.
- Ranking endpoints now infer `user_id` from the active session rather than request payloads.
- Song catalog remains globally shared while votes/scores/snapshots are isolated per user.

## Environment variables

You can configure the DB with either:

- `DATABASE_URL` (preferred)
- or the PostgreSQL parts: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`

By default, `docker-compose.yml` runs a single container and stores SQLite data at `/data/songranker.db`. That path is mounted from the named Docker volume `songranker_data`, so database state persists across container rebuilds/restarts until you remove the volume.

## APIs

- `GET /api/rate/next?artist=<optional>&title_query=<optional>&song_ids=<optional_csv>`
  - Returns the next deterministic comparison pair for the signed-in user, with optional active filters.
- `POST /api/rate/vote`
  - Body: `winner_song_id`, `loser_song_id`, and `filters` context.
  - Applies an Elo update to global per-user song ratings, records vote history, and stores rating snapshots for the vote.
- `POST /api/plex/resync`
  - Manually refreshes cached song metadata from Plex so ranking can continue even when Plex is temporarily unavailable.

A periodic background job also attempts Plex resync hourly once setup is complete.

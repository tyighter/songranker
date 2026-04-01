# SongRanker Scaffold

This scaffold includes:

- FastAPI backend with server-rendered setup and ranking pages.
- PostgreSQL database.
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

### 2) Run migrations

```bash
docker compose exec app alembic upgrade head
```

### 3) Open setup wizard

On first launch, SongRanker redirects all app traffic to `/setup` until initialization is complete.

Setup steps:

1. Create the first local user.
2. Save Plex URL and token.
3. Pick Plex music library section.
4. Run initial import to cache metadata locally.

Imported metadata includes `title`, `artist`, `album`, `year`, `decade`, and `plex_rating_key`.

## Environment variables

The app reads the following DB environment variables:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

By default, `docker-compose.yml` wires these to the `db` service.

## APIs

- `GET /api/rate/next?user_id=<id>&artist=<optional>&title_query=<optional>&song_ids=<optional_csv>`
  - Returns the next deterministic comparison pair for the user, with optional active filters.
- `POST /api/rate/vote`
  - Body: `user_id`, `winner_song_id`, `loser_song_id`, and `filters` context.
  - Applies an Elo update to global per-user song ratings, records vote history, and stores rating snapshots for the vote.
- `POST /api/plex/resync`
  - Manually refreshes cached song metadata from Plex so ranking can continue even when Plex is temporarily unavailable.

A periodic background job also attempts Plex resync hourly once setup is complete.

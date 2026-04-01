# SongRanker Scaffold

This scaffold includes:

- FastAPI backend with a minimal server-rendered frontend.
- PostgreSQL database.
- Alembic migrations for `settings`, `users`, `songs`, `pairwise_votes`, and `rating_scores`.
- First-launch onboarding flow at `/setup` (auto-redirect when app is uninitialized).
- Plex metadata import and resync support with local metadata cache in `songs`.
- Health check endpoint at `/health`.

## Quick start

### 1) Start everything

```bash
docker compose up --build
```

The app will be available at:

- http://0.0.0.0:2112
- http://localhost:2112

### 2) Complete onboarding

On first launch, requests to `/` are redirected to `/setup` where you:

1. Create the first local user.
2. Enter Plex URL and token.
3. Select a Plex music library section.
4. Run the initial track import.

### 3) Verify health

```bash
curl http://localhost:2112/health
```

Expected response:

```json
{"status":"ok"}
```

### 4) Manual resync

```bash
curl -X POST http://localhost:2112/resync
```

This refreshes cached song metadata from Plex.

## Environment variables

The app reads the following DB environment variables:

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

By default, `docker-compose.yml` wires these to the `db` service.

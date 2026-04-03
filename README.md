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

### Optional: configure Google OIDC sign-in

If you want "Continue with Google" to be active, configure Google OAuth credentials before starting the container:

1. Create a `.env` file (you can copy from `.env.example`).
2. Fill in `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
3. Set `GOOGLE_REDIRECT_URI` to your callback URL (for local default: `http://localhost:2112/api/auth/google/callback`).
4. In Google Cloud Console, add the same callback URL under your OAuth client's **Authorized redirect URIs**.
5. Restart the app:

```bash
docker compose up --build
```

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

#### Migration troubleshooting

##### Error signature: `sqlite3.OperationalError: table ... already exists`

This usually means the database schema already exists in the volume, but Alembic revision tracking is out of sync.

Decision tree:

- **If data can be discarded (fastest recovery):**
  1. Remove containers + named volume.
  2. Start fresh so startup migrations recreate the schema.

  ```bash
  docker compose down -v
  docker compose up --build
  ```

- **If data must be preserved (safe recovery):**
  1. Inspect the existing schema and migration state.
  2. Choose the Alembic revision that matches the already-existing tables.
  3. Stamp the DB to that revision (without applying DDL).
  4. Upgrade to head normally.

  ```bash
  # Current stamped revision (if any)
  docker compose exec songranker alembic current

  # Full revision list to choose from
  docker compose exec songranker alembic history

  # Mark DB as being at the chosen matching revision
  docker compose exec songranker alembic stamp <revision>

  # Apply only remaining migrations
  docker compose exec songranker alembic upgrade head
  ```

> ⚠️ Avoid mixing manual schema creation (for example, running ad-hoc `CREATE TABLE` statements) with Alembic-managed migrations. Let Alembic own schema evolution to prevent drift and repeated "already exists" failures.

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
- `YOUTUBE_DATA_API_KEY` to enable verified embeddable YouTube lookups via YouTube Data API v3.
- `YOUTUBE_SEARCH_FALLBACK_PROVIDER` to control non-API fallbacks (`disabled` by default; set to `youtube_html_scrape` only if you explicitly want unverified fallback candidates).
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` to enable Google OIDC sign-in/account linking.

By default, `docker-compose.yml` runs a single container and stores SQLite data at `/data/songranker.db`. That path is mounted from the named Docker volume `songranker_data`, so database state persists across container rebuilds/restarts until you remove the volume.

## APIs

- `GET /api/rate/next?artist=<optional>&title_query=<optional>&song_ids=<optional_csv>`
  - Returns the next deterministic comparison pair for the signed-in user, with optional active filters.
- `POST /api/rate/vote`
  - Body: `winner_song_id`, `loser_song_id`, and `filters` context.
  - Applies an Elo update to global per-user song ratings, records vote history, and stores rating snapshots for the vote.
- `POST /api/plex/resync`
  - Manually refreshes cached song metadata from Plex so ranking can continue even when Plex is temporarily unavailable.
- `GET /api/settings/plex`
  - Returns the authenticated user's Plex connection settings payload, including `popularity_weight`, connection status, and available libraries.
- `POST /api/settings/plex`
  - JSON body: `plex_url`, `plex_token`, and optional `popularity_weight`.
  - Saves Plex connection settings and returns the normalized settings payload used by JSON clients.
- `POST /api/settings/library`
  - JSON body: `plex_music_section_id`.
  - Persists the selected Plex music library section.

A periodic background job also attempts Plex resync hourly once setup is complete.

## Diagnosing Plex resync failures

SongRanker writes logs to both container stdout and `/log.log` inside the container.

Useful commands:

```bash
# Follow runtime logs from Docker
docker compose logs -f songranker

# Show the most recent lines from the in-container log file
docker compose exec songranker sh -lc "tail -n 200 /log.log"
```

When a manual resync fails (`POST /api/plex/resync`), the app now writes a full stack trace with the message:

- `Manual Plex resync failed`

Periodic hourly failures are logged with:

- `Periodic Plex sync failed`

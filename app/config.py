import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "2112"))

    db_host: str = os.getenv("DB_HOST", "db")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: str = os.getenv("DB_NAME", "songranker")
    db_user: str = os.getenv("DB_USER", "songranker")
    db_password: str = os.getenv("DB_PASSWORD", "songranker")
    database_url_override: str | None = os.getenv("DATABASE_URL")
    youtube_data_api_key: str = os.getenv("YOUTUBE_DATA_API_KEY", "")
    youtube_search_fallback_provider: str = os.getenv("YOUTUBE_SEARCH_FALLBACK_PROVIDER", "disabled")
    youtube_lookup_cache_ttl_seconds: int = int(os.getenv("YOUTUBE_LOOKUP_CACHE_TTL_SECONDS", "900"))
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_redirect_uri: str = os.getenv("GOOGLE_REDIRECT_URI", "")
    google_oidc_discovery_url: str = os.getenv(
        "GOOGLE_OIDC_DISCOVERY_URL", "https://accounts.google.com/.well-known/openid-configuration"
    )
    app_env: str = os.getenv("APP_ENV", "local").strip().lower()
    session_cookie_secure: bool = _env_bool("SESSION_COOKIE_SECURE", default=app_env not in {"local", "development"})
    session_cookie_samesite: str = (
        os.getenv("SESSION_COOKIE_SAMESITE", "lax").strip().lower()
        if os.getenv("SESSION_COOKIE_SAMESITE")
        else "lax"
    )
    session_cookie_domain: str | None = os.getenv("SESSION_COOKIE_DOMAIN")
    session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "30"))
    allow_auth_switch_user: bool = _env_bool(
        "ALLOW_AUTH_SWITCH_USER",
        default=app_env in {"local", "development"},
    )

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}@"
            f"{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def normalized_session_cookie_samesite(self) -> str:
        value = self.session_cookie_samesite.strip().lower()
        if value not in {"lax", "strict"}:
            return "lax"
        return value

    @property
    def normalized_session_cookie_domain(self) -> str | None:
        if not self.session_cookie_domain:
            return None
        normalized = self.session_cookie_domain.strip()
        return normalized or None


settings = Settings()

"""Environment-variable configuration, in the same plain-function style as models.mongodb_url().

MONGODB_URL / MONGODB_DB are read by models.py and reused as-is; only the API's own settings
live here.
"""

import os


def redis_url():
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def cors_origins():
    """Comma-separated CORS_ORIGINS. Empty (the default) means no browser origin is allowed --
    never "allow all"."""
    raw = os.environ.get("CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def events_channel():
    return "charging-events"

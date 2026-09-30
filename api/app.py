"""The public, read-only API (08-public-api-service.md). Run with `uvicorn api.app:app`.

Never imports main.py; its only channels to the Central System are MongoDB and Redis pub/sub.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError

from api import config
from api.live import ConnectionManager, subscribe_forever
from api.routes_rest import router as rest_router
from api.routes_ws import router as ws_router
from models import display_mongodb_url, init_db


@asynccontextmanager
async def lifespan(app):
    try:
        client = await init_db()
    except PyMongoError as exc:
        print(f"Could not reach MongoDB at {display_mongodb_url()}: {type(exc).__name__}")
        print("Start MongoDB, or set MONGODB_URL to point elsewhere.")
        raise SystemExit(1) from exc

    app.state.manager = ConnectionManager()
    app.state.redis = None
    app.state.subscriber = None
    redis_client = redis_asyncio.Redis.from_url(config.redis_url())
    try:
        await redis_client.ping()
    except Exception as exc:
        logging.warning(
            "Redis unreachable at %s (%s): REST works, WebSockets are refused",
            config.redis_url(), type(exc).__name__,
        )
        await redis_client.aclose()
    else:
        app.state.redis = redis_client
        ready = asyncio.Event()
        app.state.subscriber = asyncio.create_task(
            subscribe_forever(redis_client, config.events_channel(), app.state.manager, ready)
        )
        await ready.wait()
    try:
        yield
    finally:
        if app.state.subscriber is not None:
            app.state.subscriber.cancel()
            await asyncio.gather(app.state.subscriber, return_exceptions=True)
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await client.close()


app = FastAPI(title="Charging public API", version="1", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(rest_router)
app.include_router(ws_router)

"""Redis subscriber loop plus the in-process registry of open WebSockets (§E, §G)."""

import asyncio
import json
import logging


class ConnectionManager:
    """Open sockets, each with the set of charge point identities it cares about (resolved once
    at connect time, so no MongoDB query per incoming event)."""

    def __init__(self):
        self._connections = {}  # websocket -> frozenset of identities

    def add(self, websocket, identities):
        self._connections[websocket] = frozenset(identities)

    def remove(self, websocket):
        self._connections.pop(websocket, None)

    def __len__(self):
        return len(self._connections)

    async def dispatch(self, event):
        identity = event.get("charge_point_identity")
        for websocket, identities in list(self._connections.items()):
            if identity not in identities:
                continue
            try:
                await websocket.send_json(event)
            except Exception:
                # A socket that vanished mid-send: drop it, it is not an error worth logging.
                self.remove(websocket)


async def subscribe_forever(redis_client, channel, manager, ready=None):
    """Feed every message on `channel` to the manager until cancelled. Reconnects after a
    Redis failure rather than dying silently."""
    while True:
        try:
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(channel)
            if ready is not None:
                ready.set()
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                except (TypeError, ValueError):
                    logging.warning("ignoring malformed event on %s", channel)
                    continue
                await manager.dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.warning("Redis subscriber failed; retrying in 2s", exc_info=True)
            await asyncio.sleep(2)

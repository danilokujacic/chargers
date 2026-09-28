"""WebSocket endpoints (§G)."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api import queries

router = APIRouter(prefix="/api/v1/ws")


async def _stream(websocket, initial, identities):
    manager = websocket.app.state.manager
    await websocket.accept()
    await websocket.send_json(initial.model_dump(mode="json"))
    manager.add(websocket, identities)
    try:
        while True:
            # Clients send nothing meaningful; receiving just detects the disconnect.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(websocket)


async def _refuse(websocket, code, reason):
    """Accept, then close with a real close code: closing before accept is turned by the
    server into a bare HTTP 403, which would lose the code and reason §G requires."""
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


def _redis_down(websocket):
    return websocket.app.state.subscriber is None


@router.websocket("/sites/{site_id}")
async def ws_site(websocket: WebSocket, site_id: str):
    if _redis_down(websocket):
        await _refuse(websocket, 1013, "live updates unavailable, try again later")
        return
    try:
        detail = await queries.site_detail(site_id)
    except queries.InvalidSiteId:
        detail = None
    if detail is None:
        await _refuse(websocket, 1008, "no such site")
        return
    if detail.source == "external_reference":
        await _refuse(
            websocket, 1008, "no operator-managed charger at this location; nothing live to send"
        )
        return
    await _stream(websocket, detail, {cp.identity for cp in detail.charge_points})


@router.websocket("/charge-points/{identity}")
async def ws_charge_point(websocket: WebSocket, identity: str):
    if _redis_down(websocket):
        await _refuse(websocket, 1013, "live updates unavailable, try again later")
        return
    detail = await queries.charge_point_detail(identity)
    if detail is None:
        await _refuse(websocket, 1008, "no such charge point")
        return
    await _stream(websocket, detail, {identity})

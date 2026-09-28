"""REST endpoints (§F). GET only -- nothing here can change charger state."""

from fastapi import APIRouter, HTTPException

from api import queries
from api.schemas import ChargePointDetail, SiteDetail, SiteSummary

router = APIRouter(prefix="/api/v1")


@router.get("/sites", response_model=list[SiteSummary])
async def get_sites():
    return await queries.list_sites()


@router.get("/sites/{site_id}", response_model=SiteDetail)
async def get_site(site_id: str):
    try:
        detail = await queries.site_detail(site_id)
    except queries.InvalidSiteId:
        raise HTTPException(status_code=422, detail=f"{site_id!r} is not a valid site id")
    if detail is None:
        raise HTTPException(status_code=404, detail="no such site")
    return detail


@router.get("/charge-points/{identity}", response_model=ChargePointDetail)
async def get_charge_point(identity: str):
    detail = await queries.charge_point_detail(identity)
    if detail is None:
        raise HTTPException(status_code=404, detail="no such charge point")
    return detail

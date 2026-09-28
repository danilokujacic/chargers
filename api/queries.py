"""Read-only Beanie queries shared by the REST and WebSocket layers. Nothing here writes."""

from beanie import PydanticObjectId
from bson.errors import InvalidId

from api.schemas import (
    ChargePointDetail,
    ChargePointInSite,
    ConnectorOut,
    CurrentTransaction,
    SiteDetail,
    SiteSummary,
)
from models import ChargePoint, ConnectorStatus, Site, SiteSource, Transaction

STANDALONE_PREFIX = "standalone:"
IN_SESSION = {"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"}


class InvalidSiteId(ValueError):
    pass


def aggregate_status(source, statuses):
    """§F's precedence, first match wins. `statuses` are plain status strings."""
    if source == SiteSource.external_reference:
        return "unknown"
    if "Faulted" in statuses:
        return "faulted"
    if "Available" in statuses:
        return "available"
    if "Reserved" in statuses:
        return "reserved"
    if any(status in IN_SESSION for status in statuses):
        return "occupied"
    return "unavailable"


def _connector_outs(records):
    return [
        ConnectorOut(
            connector_id=r.connector_id, status=r.status.value, error_code=r.error_code.value
        )
        for r in sorted(records, key=lambda r: r.connector_id)
        if r.connector_id != 0
    ]


def _aggregate_statuses(records):
    """Connector 0 is the charger as a whole: it only matters here when it is Faulted."""
    return [
        r.status.value for r in records if r.connector_id != 0 or r.status.value == "Faulted"
    ]


def parse_site_id(site_id):
    """Returns ("standalone", identity) or ("site", ObjectId); raises InvalidSiteId."""
    if site_id.startswith(STANDALONE_PREFIX):
        return "standalone", site_id[len(STANDALONE_PREFIX):]
    try:
        return "site", PydanticObjectId(site_id)
    except (InvalidId, TypeError) as exc:
        raise InvalidSiteId(site_id) from exc


async def _connectors_by_identity(identities=None):
    query = ConnectorStatus.find_all() if identities is None else ConnectorStatus.find(
        {"charge_point_identity": {"$in": list(identities)}}
    )
    grouped = {}
    for record in await query.to_list():
        grouped.setdefault(record.charge_point_identity, []).append(record)
    return grouped


async def list_sites():
    sites = await Site.find_all().to_list()
    charge_points = await ChargePoint.find_all().to_list()
    connectors = await _connectors_by_identity()
    site_ids = {site.id for site in sites}

    by_site = {}
    standalone = []
    for cp in charge_points:
        if cp.site_id is not None and cp.site_id in site_ids:
            by_site.setdefault(cp.site_id, []).append(cp)
        elif cp.latitude is not None and cp.longitude is not None:
            standalone.append(cp)

    result = []
    for site in sites:
        members = by_site.get(site.id, [])
        if site.source == SiteSource.external_reference:
            count = site.external_charge_point_count or 0
            statuses = []
        else:
            count = len(members)
            statuses = [
                s for cp in members for s in _aggregate_statuses(connectors.get(cp.identity, []))
            ]
        result.append(
            SiteSummary(
                id=str(site.id),
                name=site.name,
                site_type=site.site_type.value,
                source=site.source.value,
                latitude=site.latitude,
                longitude=site.longitude,
                connector_types=site.connector_types,
                charge_point_count=count,
                aggregate_status=aggregate_status(site.source, statuses),
            )
        )
    for cp in standalone:
        result.append(
            SiteSummary(
                id=STANDALONE_PREFIX + cp.identity,
                name=cp.identity,
                site_type="standalone",
                source=SiteSource.operator.value,
                latitude=cp.latitude,
                longitude=cp.longitude,
                connector_types=None,
                charge_point_count=1,
                aggregate_status=aggregate_status(
                    SiteSource.operator, _aggregate_statuses(connectors.get(cp.identity, []))
                ),
            )
        )
    return result


async def _charge_points_in(members):
    connectors = await _connectors_by_identity(cp.identity for cp in members)
    return [
        ChargePointInSite(
            identity=cp.identity, connectors=_connector_outs(connectors.get(cp.identity, []))
        )
        for cp in sorted(members, key=lambda cp: cp.identity)
    ]


async def site_detail(site_id):
    """SiteDetail for a site id, or None if it does not exist. Raises InvalidSiteId."""
    kind, key = parse_site_id(site_id)
    if kind == "standalone":
        cp = await ChargePoint.find_one(ChargePoint.identity == key)
        if cp is None or cp.site_id is not None and await Site.get(cp.site_id) is not None:
            return None
        if cp.latitude is None or cp.longitude is None:
            return None
        return SiteDetail(
            id=site_id,
            name=cp.identity,
            site_type="standalone",
            source=SiteSource.operator.value,
            latitude=cp.latitude,
            longitude=cp.longitude,
            charge_points=await _charge_points_in([cp]),
        )
    site = await Site.get(key)
    if site is None:
        return None
    members = []
    if site.source == SiteSource.operator:
        members = await ChargePoint.find(ChargePoint.site_id == site.id).to_list()
    return SiteDetail(
        id=str(site.id),
        name=site.name,
        site_type=site.site_type.value,
        source=site.source.value,
        latitude=site.latitude,
        longitude=site.longitude,
        address=site.address,
        connector_types=site.connector_types,
        charge_points=await _charge_points_in(members),
    )


def _latest_reading(meter_values):
    """Most recent sampled value as an int (Wh), preferring the energy register."""
    for meter_value in reversed(meter_values):
        samples = meter_value.get("sampled_value") or []
        preferred = [
            s for s in samples
            if s.get("measurand") in (None, "Energy.Active.Import.Register")
        ]
        for sample in preferred or samples:
            try:
                return int(float(sample["value"]))
            except (KeyError, TypeError, ValueError):
                continue
    return None


async def charge_point_detail(identity):
    cp = await ChargePoint.find_one(ChargePoint.identity == identity)
    if cp is None:
        return None
    connectors = (await _connectors_by_identity([identity])).get(identity, [])
    transaction = await Transaction.find(
        Transaction.charge_point_identity == identity, Transaction.is_open == True  # noqa: E712
    ).sort(-Transaction.transaction_id).first_or_none()
    current = None
    if transaction is not None:
        current = CurrentTransaction(
            transaction_id=transaction.transaction_id,
            meter_start=transaction.meter_start,
            latest_meter_value=_latest_reading(transaction.meter_values),
        )
    return ChargePointDetail(
        identity=identity,
        site_id=str(cp.site_id) if cp.site_id is not None else None,
        connectors=_connector_outs(connectors),
        current_transaction=current,
    )



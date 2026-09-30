"""Public, versioned response models (08-public-api-service.md §F). Deliberately separate from
the Beanie documents in models.py so an internal-only field added there never leaks out."""

from pydantic import BaseModel


class SiteSummary(BaseModel):
    id: str
    name: str
    site_type: str
    # "operator", "external_reference", or "simulated" (a demo site from 11-demo-fleet.md,
    # live exactly like "operator").
    source: str
    latitude: float
    longitude: float
    connector_types: list[str] | None = None
    charge_point_count: int
    aggregate_status: str


class ConnectorOut(BaseModel):
    connector_id: int
    status: str
    error_code: str
    # The hardware, from the charger's ConnectorSpec: plug (e.g. "CCS2"), "AC"/"DC", and rated
    # kW. None when nobody described this connector, or (kW only) when its rating is unknown.
    connector_type: str | None = None
    power_type: str | None = None
    max_power_kw: float | None = None


class ChargePointInSite(BaseModel):
    identity: str
    connectors: list[ConnectorOut]


class SiteDetail(BaseModel):
    id: str
    name: str
    site_type: str
    source: str  # as in SiteSummary
    latitude: float
    longitude: float
    address: str | None = None
    connector_types: list[str] | None = None
    charge_points: list[ChargePointInSite]


class CurrentTransaction(BaseModel):
    transaction_id: int
    meter_start: int
    latest_meter_value: int | None = None


class ChargePointDetail(BaseModel):
    identity: str
    site_id: str | None = None
    connectors: list[ConnectorOut]
    current_transaction: CurrentTransaction | None = None

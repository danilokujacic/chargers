"""Public, versioned response models (08-public-api-service.md §F). Deliberately separate from
the Beanie documents in models.py so an internal-only field added there never leaks out."""

from pydantic import BaseModel


class SiteSummary(BaseModel):
    id: str
    name: str
    site_type: str
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


class ChargePointInSite(BaseModel):
    identity: str
    connectors: list[ConnectorOut]


class SiteDetail(BaseModel):
    id: str
    name: str
    site_type: str
    source: str
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

"""Validated runtime options persisted without credentials in AppSetting."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import AppSetting, Availability, PriceAccess


class MonitoringConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduler_enabled: bool = True
    priority_interval_seconds: int = Field(default=60, ge=1, le=86400)
    general_interval_seconds: int = Field(default=300, ge=1, le=86400)
    provider_min_interval_seconds: Decimal = Field(default=Decimal("1"), ge=0, le=3600)
    price_max_age_seconds: int = Field(default=600, ge=1, le=86400)
    score_price_drop: Decimal = Field(default=Decimal("40"), ge=0, le=100000)
    score_confirmed_coupon: Decimal = Field(default=Decimal("30"), ge=0, le=100000)
    score_priority: Decimal = Field(default=Decimal("20"), ge=0, le=100000)
    score_discount_percent: Decimal = Field(default=Decimal("1"), ge=0, le=100000)
    score_availability: Decimal = Field(default=Decimal("5"), ge=0, le=100000)
    mock_current_price: Decimal = Field(
        default=Decimal("99.90"), gt=0, max_digits=12, decimal_places=2
    )
    mock_previous_price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    mock_availability: Availability = Availability.AVAILABLE
    mock_price_access: PriceAccess = PriceAccess.PUBLIC


CONFIG_KEY = "monitoring_config"


def load_monitoring_settings(session: Session, settings: Settings) -> Settings:
    row = session.scalar(select(AppSetting).where(AppSetting.key == CONFIG_KEY))
    if row is None:
        return settings
    configuration = MonitoringConfiguration.model_validate(row.value)
    return settings.model_copy(update=configuration.model_dump())


def read_monitoring_configuration(session: Session, settings: Settings) -> MonitoringConfiguration:
    effective = load_monitoring_settings(session, settings)
    return MonitoringConfiguration.model_validate(
        {key: getattr(effective, key) for key in MonitoringConfiguration.model_fields}
    )


def save_monitoring_configuration(
    session: Session, settings: Settings, updates: dict
) -> MonitoringConfiguration:
    configuration = MonitoringConfiguration.model_validate(
        {**read_monitoring_configuration(session, settings).model_dump(), **updates}
    )
    row = session.scalar(select(AppSetting).where(AppSetting.key == CONFIG_KEY))
    if row is None:
        row = AppSetting(key=CONFIG_KEY, value=configuration.model_dump(mode="json"))
        session.add(row)
    else:
        row.value = configuration.model_dump(mode="json")
    session.flush()
    return configuration

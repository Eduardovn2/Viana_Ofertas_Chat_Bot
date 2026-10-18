from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Availability, Platform, PriceAccess


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductCreate(InputModel):
    platform: Platform
    name: str = Field(min_length=1, max_length=255)
    original_url: str = Field(min_length=1, max_length=2048)
    external_id: str | None = Field(default=None, min_length=1, max_length=100)
    monitoring_enabled: bool = True
    priority: bool = False


class ProductUpdate(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    original_url: str | None = Field(default=None, min_length=1, max_length=2048)
    monitoring_enabled: bool | None = None
    priority: bool | None = None

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("Campos fornecidos não podem ser nulos")
        return self


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    platform: Platform
    external_id: str | None
    name: str
    original_url: str
    monitoring_enabled: bool
    priority: bool
    affiliate_pending: bool
    created_at: datetime
    updated_at: datetime


class MonitoringUpdate(InputModel):
    enabled: bool


class AffiliateLinkWrite(InputModel):
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("URL contém caracteres de controle")
        return value


class AffiliateLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    url: str
    source: str
    created_at: datetime
    updated_at: datetime


class PriceHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    current_price: Decimal | None
    previous_price: Decimal | None
    currency: str
    availability: Availability
    price_access: PriceAccess
    source: str
    is_simulated: bool
    observed_at: datetime
    created_at: datetime
    installment_text: str | None
    shipping_text: str | None

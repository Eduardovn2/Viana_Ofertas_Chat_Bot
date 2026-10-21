"""Fail closed unless both the execution context and data are simulation-only."""

import re
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select, text

from app.models import (
    DestinationChannel,
    Product,
    PublicationDestination,
    SimulationPolicy,
    SimulationProduct,
)
from app.services.categories import normalize_text


def require_simulation_scope(session, settings):
    if (
        settings.telegram_publisher_enabled
        or settings.telegram_real_sends_authorized
        or settings.mercado_livre_enabled
        or settings.shopee_enabled
        or settings.price_provider != "simulated"
    ):
        raise ValueError("Simulação exige publicadores e conectores reais desativados.")
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        database, schema = session.execute(
            text("SELECT current_database(), current_schema()")
        ).one()
        if database != "ofertas_bot_test" or not re.fullmatch(
            r"ofertas_bot_\w*_test_[a-f0-9]{32}", schema
        ):
            raise ValueError("Use schema UUID isolado em ofertas_bot_test; nunca public/principal.")
    elif bind.dialect.name != "sqlite" or (
        settings.app_env != "test" and not str(bind.url.database).endswith(".simulation.db")
    ):
        raise ValueError("Use SQLite de testes ou arquivo dedicado .simulation.db.")


Identifier = Annotated[int, Field(gt=0, strict=True)]
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


class SimulationPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = False
    automatic_approval: StrictBool = False
    product_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    terms: list[str] = Field(default_factory=list, max_length=50)
    max_price: Money | None = None
    min_discount: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)

    @field_validator("terms")
    @classmethod
    def terms_valid(cls, values):
        if any(
            not value.strip()
            or len(value) > 100
            or any(ord(c) < 32 for c in value)
            or not normalize_text(value)
            for value in values
        ):
            raise ValueError("Termos específicos inválidos.")
        normalized = [normalize_text(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Termos repetidos.")
        return normalized

    @field_validator("product_ids")
    @classmethod
    def ids_unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Produtos repetidos.")
        return values


class SimulationEnrollmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prices: list[Money | None] = Field(min_length=1, max_length=100)


def configure_policy(session, settings, destination, data):
    require_simulation_scope(session, settings)
    # Same first lock as the dispatcher: a policy edit cannot commit between
    # revalidation of an approved preview and its durable claim.
    destination = session.scalar(
        select(PublicationDestination)
        .where(PublicationDestination.id == destination.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if destination is None:
        raise ValueError("Destino inexistente.")
    if destination.channel != DestinationChannel.SIMULATED:
        raise ValueError("Filtros automáticos desta fase exigem destino SIMULATED.")
    data = SimulationPolicyInput.model_validate(data)
    if set(data.product_ids) != set(
        session.scalars(
            select(SimulationProduct.product_id).where(
                SimulationProduct.product_id.in_(data.product_ids)
            )
        )
    ):
        raise ValueError("Selecione somente produtos fictícios inscritos nesta simulação.")
    policy = session.get(SimulationPolicy, destination.id) or SimulationPolicy(
        destination_id=destination.id
    )
    for field, value in data.model_dump().items():
        setattr(policy, field, value)
    session.add(policy)
    session.flush()
    return policy


def enroll_product(session, settings, product_id, data):
    require_simulation_scope(session, settings)
    data = SimulationEnrollmentInput.model_validate(data)
    product = session.get(Product, product_id)
    if (
        product is None
        or not product.name.startswith("[SIMULATED] ")
        or (product.classification_metadata or {}).get("simulation_only") is not True
    ):
        raise ValueError("Inscrição exige produto explicitamente fictício [SIMULATED].")
    profile = session.get(SimulationProduct, product_id) or SimulationProduct(product_id=product_id)
    profile.prices = [str(price) if price is not None else None for price in data.prices]
    session.add(profile)
    session.flush()
    return profile

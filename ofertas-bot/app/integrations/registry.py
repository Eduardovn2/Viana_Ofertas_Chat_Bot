"""Lazy connectors: construction/startup never performs commercial API requests."""

import hashlib
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from sqlalchemy import delete, select

from app.core.config import Settings
from app.integrations.contracts import ProductRef
from app.integrations.errors import AuthorizationDenied, IntegrationError, InvalidPayload
from app.integrations.http import JsonTransport
from app.models import AppSetting, MonitoringLease, Platform, Product
from app.services.leases import acquire_lease, conflict_insert, lease_expires_at


class ConnectorProvider:
    def __init__(self, connector):
        self.connector = connector

    def fetch(self, product: Product):
        if not product.external_id:
            raise InvalidPayload("Cadastre o identificador oficial do produto antes da consulta.")
        return self.connector.fetch_product(
            ProductRef(
                product.platform, product.external_id, product.original_url, product.variant_id
            )
        )


class ConnectorRegistry:
    def __init__(self, settings: Settings, *, session_factory=None, clock=None):
        self.settings = settings
        self.session_factory = session_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self._connectors = {}
        self._transports = []
        self._lock = RLock()
        self.blocked = set()

    def configured(self, platform: Platform) -> bool:
        if platform == Platform.MERCADO_LIVRE:
            return bool(
                self.settings.mercado_livre_access_token
                and self.settings.mercado_livre_access_token.get_secret_value().strip()
            )
        return bool(
            self.settings.shopee_app_id
            and self.settings.shopee_secret
            and self.settings.shopee_app_id.get_secret_value().strip()
            and self.settings.shopee_secret.get_secret_value().strip()
        )

    def enabled(self, platform: Platform) -> bool:
        flag = (
            self.settings.mercado_livre_enabled
            if platform == Platform.MERCADO_LIVRE
            else self.settings.shopee_enabled
        )
        return flag and self.configured(platform) and not self.is_blocked(platform)

    def _block_key(self, platform):
        return f"integration:block:{platform.value}:{self.credential_scope(platform)}"

    def is_blocked(self, platform: Platform) -> bool:
        if self.session_factory is None:
            return platform in self.blocked
        with self.session_factory() as session:
            return bool(
                session.scalar(
                    select(AppSetting.id).where(AppSetting.key == self._block_key(platform))
                )
            )

    def block(self, platform: Platform):
        self.blocked.add(platform)
        if self.session_factory is not None:
            with self.session_factory() as session:
                session.execute(
                    conflict_insert(session, AppSetting)
                    .values(
                        key=self._block_key(platform),
                        value={"code": "AUTHORIZATION_DENIED"},
                        description="Recusa da API; liberar somente após check explícito bem-sucedido.",
                    )
                    .on_conflict_do_nothing(index_elements=[AppSetting.key])
                )
                session.commit()

    def unblock(self, platform: Platform):
        if self.session_factory is not None:
            with self.session_factory() as session:
                session.execute(
                    delete(AppSetting).where(AppSetting.key == self._block_key(platform))
                )
                session.commit()
        self.blocked.discard(platform)

    def _defer_rate(self, platform: Platform, delay: float):
        if self.session_factory is None:
            return
        until = self.clock() + timedelta(seconds=delay)
        name = f"api:{platform.value}:{self.credential_scope(platform)}"
        with self.session_factory() as session:
            statement = conflict_insert(session, MonitoringLease).values(
                name=name, owner=uuid4().hex, expires_at=until, updated_at=self.clock()
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[MonitoringLease.name],
                    set_={"expires_at": until, "updated_at": self.clock()},
                    where=MonitoringLease.expires_at < until,
                )
            )
            session.commit()

    def credential_scope(self, platform: Platform) -> str:
        secret = (
            self.settings.mercado_livre_access_token
            if platform == Platform.MERCADO_LIVRE
            else self.settings.shopee_app_id
        )
        # Only a nonreversible hash goes into the internal lease key, never logs or API output.
        return hashlib.sha256(
            (secret.get_secret_value() if secret else "disabled").encode()
        ).hexdigest()[:24]

    def _rate_gate(self, platform: Platform):
        if self.session_factory is None:
            return
        name = f"api:{platform.value}:{self.credential_scope(platform)}"
        owner = acquire_lease(
            self.session_factory, name, self.settings.integration_min_interval_seconds, self.clock
        )
        if owner is None:
            retry_at = lease_expires_at(self.session_factory, name)
            error = IntegrationError("RATE_LIMITED", "Intervalo da credencial ainda não decorrido.")
            error.retry_at = retry_at
            raise error

    def get(self, platform: Platform):
        with self._lock:
            if platform not in self._connectors:
                transport = JsonTransport(
                    timeout=self.settings.integration_timeout_seconds,
                    max_retries=self.settings.integration_max_retries,
                    min_interval=self.settings.integration_min_interval_seconds,
                    cache_seconds=self.settings.integration_cache_seconds,
                    max_retry_wait=self.settings.integration_max_retry_wait_seconds,
                    clock=self.clock,
                    rate_gate=lambda: self._rate_gate(platform),
                    rate_defer=lambda delay: self._defer_rate(platform, delay),
                    on_auth_failure=lambda: self.block(platform),
                )
                self._transports.append(transport)
                if platform == Platform.MERCADO_LIVRE:
                    from app.integrations.mercado_livre import MercadoLivreConnector

                    connector = MercadoLivreConnector(
                        transport,
                        token=self.settings.mercado_livre_access_token,
                        enabled=self.settings.mercado_livre_enabled,
                        clock=self.clock,
                    )
                else:
                    from app.integrations.shopee import ShopeeConnector

                    connector = ShopeeConnector(
                        transport,
                        app_id=self.settings.shopee_app_id,
                        secret=self.settings.shopee_secret,
                        enabled=self.settings.shopee_enabled,
                        short_links_enabled=self.settings.shopee_short_links_enabled,
                        clock=self.clock,
                    )
                self._connectors[platform] = connector
            return self._connectors[platform]

    def provider(self, product: Product, *, allow_blocked: bool = False):
        if not allow_blocked and self.is_blocked(product.platform):
            raise AuthorizationDenied(
                "Conector pausado após recusa de autorização; faça check explícito após corrigir o acesso."
            )
        return ConnectorProvider(self.get(product.platform))

    def close(self):
        for transport in self._transports:
            transport.close()

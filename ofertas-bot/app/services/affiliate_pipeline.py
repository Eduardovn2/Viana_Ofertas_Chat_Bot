"""Affiliate preparation and scoped finalization in the existing offer pipeline.

Durable reservations use AppSetting's unique key and the shared lease mechanism.
Laboratory transport guards remain separate from expressly approved real batches.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from app.integrations.contracts import AffiliateLinkResult, fingerprint
from app.integrations.errors import IntegrationError, InvalidPayload
from app.integrations.http import JsonTransport
from app.integrations.shopee import ENDPOINT, ShopeeConnector, _link_target_verified, _url_identity
from app.models import (
    AffiliateLink,
    AppSetting,
    Availability,
    DestinationChannel,
    IntegrationEvent,
    Platform,
    PriceAccess,
    Product,
    PublicationDestination,
    PublicationQueueItem,
)
from app.services.category_routing import destination_accepts
from app.services.comparison import PriceComparisonService, PriceDirection
from app.services.discovery_operations import DiscoveryOperations
from app.services.leases import acquire_lease, conflict_insert, release_lease, renew_lease
from app.services.offers import has_own_affiliate_link, latest_observation
from app.services.products import validate_affiliate_url
from app.services.publication_queue import SimulatedQueueService
from app.services.simulation_safety import require_simulation_scope

PREFIX = "shopee_link_mock:"
OPERATION = "generateShortLink"


def block_unreserved_real_generation():
    """The unscoped legacy route never falls through to connector I/O.

    The historical one-shot authorization is consumed and cannot be supplied here.
    Laboratory generation remains sealed to its durable MOCK_ONLY reservations.
    New real batches require CommercialLinkFinalizer's separate signed grant.
    """
    raise IntegrationError(
        "REAL_GENERATION_BLOCKED", "Geração real sem autorização durável bloqueada."
    )


class AffiliateGenerator(Protocol):
    def require_offline(self) -> None: ...

    def generate(self, origin: str) -> AffiliateLinkResult: ...

    def payload_sha256(self, origin: str) -> str: ...


class MockShopeeLinkGenerator:
    """Use the real signing/parser contract, with HTTP sealed to MockTransport."""

    def __init__(self, connector: ShopeeConnector):
        self.connector = connector
        self._lock = Lock()

    @staticmethod
    def query(origin):
        return (
            "mutation { generateShortLink(input:{originUrl:"
            + json.dumps(origin, ensure_ascii=False)
            + "}) { shortLink longLink } }"
        )

    @classmethod
    def payload_sha256(cls, origin):
        body = json.dumps(
            {"query": cls.query(origin)}, ensure_ascii=False, separators=(",", ":")
        ).encode()
        return hashlib.sha256(body).hexdigest()

    def require_offline(self):
        c = self.connector
        if (
            type(c) is not ShopeeConnector
            or type(c.transport.client._transport_for_url(httpx.URL(ENDPOINT)))
            is not httpx.MockTransport
            or not c.enabled
            or not c.short_links_enabled
            or not c.credentials_configured
        ):
            raise ValueError("Affiliate generation requires an explicitly injected mock connector.")

    def generate(self, origin):
        # A connector's HTTP hooks and signing transport are shared resources.
        with self._lock:
            return self._generate(origin)

    def _generate(self, origin):
        self.require_offline()
        c = self.connector
        c.transport.max_retries = 0
        c.transport.cache_seconds = 0
        c.transport.max_retry_wait = 0
        expected = self.query(origin)
        calls = [0]

        def guard(request):
            if (
                calls[0]
                or request.method != "POST"
                or str(request.url) != ENDPOINT
                or json.loads(request.content) != {"query": expected}
            ):
                raise InvalidPayload("Mock operation exceeded its exact-content reservation.")
            calls[0] += 1

        c.transport.client.event_hooks["request"].append(guard)
        try:
            return c.generate_affiliate_link(origin)
        finally:
            c.transport.client.event_hooks["request"].remove(guard)


class AffiliatePipelineService:
    def __init__(self, factory, settings, generator: AffiliateGenerator, *, clock=None):
        self.factory, self.settings, self.generator = factory, settings, generator
        self.clock = clock or (lambda: datetime.now(UTC))

    def require_offline(self):
        if any(
            getattr(self.settings, name)
            for name in (
                "shopee_enabled",
                "mercado_livre_enabled",
                "telegram_publisher_enabled",
                "telegram_real_sends_authorized",
                "simulation_automation_enabled",
                "shopee_short_links_enabled",
            )
        ):
            raise ValueError("Affiliate preparation requires all real controls disabled.")
        with self.factory() as session:
            require_simulation_scope(session, self.settings)
        if not isinstance(self.generator, MockShopeeLinkGenerator):
            raise ValueError("Only the sealed mock affiliate adapter is available.")
        self.generator.require_offline()

    @staticmethod
    def _key(product, destination_id):
        data = [
            product.id,
            product.external_id,
            product.variant_id,
            product.original_url,
            destination_id,
            OPERATION,
        ]
        digest = hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()
        return PREFIX + digest

    def _audit(self, session, key, value, status, error_code=None):
        session.add(
            IntegrationEvent(
                product_id=value["product_id"],
                platform=Platform.SHOPEE,
                operation="affiliate_link_mock",
                status=status,
                error_code=error_code,
                source_reference=f"{key}:destination:{value['destination_id']}",
                observed_at=self.clock(),
            )
        )

    @staticmethod
    def _row(session, key):
        return session.scalar(
            select(AppSetting)
            .where(AppSetting.key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def read(self, key):
        """Sanitized operational result; raw links never enter reports or logs."""
        with self.factory() as session:
            value = session.scalar(select(AppSetting.value).where(AppSetting.key == key))
            if value is None:
                raise LookupError("Affiliate reservation not found.")
            return {
                k: value.get(k)
                for k in (
                    "status",
                    "product_id",
                    "destination_id",
                    "history_id",
                    "attempts",
                    "product_identity_verified",
                    "account_verified",
                    "error_code",
                    "result_fingerprint",
                    "payload_sha256",
                    "mode",
                )
            } | {"reservation": key, "publication_authorized": False, "real_calls": 0}

    def _eligible(self, session, product_id, destination_id, history_id):
        p = session.get(Product, product_id)
        d = session.get(PublicationDestination, destination_id)
        h = latest_observation(session, product_id)
        if (
            p is None
            or p.platform != Platform.SHOPEE
            or p.variant_id
            or d is None
            or not d.enabled
            or d.channel != DestinationChannel.SIMULATED
            or not d.manual_approval_required
            or h is None
            or h.id != history_id
            or h.current_price is None
            or h.availability != Availability.AVAILABLE
            or h.price_access != PriceAccess.PUBLIC
            or not destination_accepts(session, p, d)
            or PriceComparisonService()
            .compare(
                session, h, now=self.clock(), max_age_seconds=self.settings.price_max_age_seconds
            )
            .direction
            != PriceDirection.DROP
        ):
            raise InvalidPayload(
                "Candidate lacks fresh, comparable and permitted commercial evidence."
            )
        preferences, _ = DiscoveryOperations().load(session, destination_id)
        if (
            not preferences.affiliate_links_simulation_enabled
            or preferences.max_affiliate_links_per_evaluation == 0
            or not preferences.open_at(self.clock())
            or session.scalar(
                select(PublicationQueueItem.id).where(
                    PublicationQueueItem.product_id == product_id,
                    PublicationQueueItem.destination_id == destination_id,
                    PublicationQueueItem.active_slot == 1,
                )
            )
            is not None
            or SimulatedQueueService(self.settings, self.clock).candidate_cooldown_issue(
                session, product_id, history_id, destination_id, self.clock()
            )
            is not None
        ):
            raise InvalidPayload(
                "Candidate is paused, duplicated or inside republication cooldown."
            )
        identity = _url_identity(p.original_url)
        parsed = urlsplit(p.original_url)
        if parsed.query or unquote(parsed.path).startswith("/universal-link/"):
            raise InvalidPayload("Use the canonical product origin, never an affiliate link.")
        if p.external_id != ":".join(identity):
            raise InvalidPayload("Canonical origin and Shopee product identity differ.")
        return p, identity

    def process(self, product_id, destination_id, history_id, *, lease_seconds=None):
        self.require_offline()
        with self.factory() as session:
            product, identity = self._eligible(session, product_id, destination_id, history_id)
            key = self._key(product, destination_id)
            if lease_seconds is None:
                lease_seconds = DiscoveryOperations().load(session)[0].lease_seconds
            if has_own_affiliate_link(product, require_verified=True):
                return {"status": "PREAPPROVED", "attempts": 0, "real_calls": 0}
            # Never overwrite a supplied/manual link, even if not yet approved.
            if product.affiliate_link is not None:
                return {"status": "BLOCKED", "error_code": "EXISTING_LINK_REVIEW_REQUIRED"}
        lease = "affiliate:" + key[len(PREFIX) :]
        owner = acquire_lease(self.factory, lease, lease_seconds, self.clock)
        if owner is None:
            return {"status": "BUSY", "attempts": 0, "real_calls": 0}
        try:
            with self.factory() as session:
                product, identity = self._eligible(session, product_id, destination_id, history_id)
                if self._key(product, destination_id) != key:
                    raise InvalidPayload("Canonical origin changed before reservation.")
                row = self._row(session, key)
                if row is not None:
                    # Holding a fresh lease means any earlier unfinished worker
                    # lost ownership. Never reclaim or repeat its operation.
                    if row.value["status"] in {"RESERVED", "CLAIMED"}:
                        row.value = {**row.value, "status": "UNCERTAIN", "error_code": "ABANDONED"}
                        self._audit(session, key, row.value, "UNCERTAIN", "ABANDONED")
                        session.commit()
                    return self.read(key) | {"reused": True}
                value = {
                    "mode": "MOCK_ONLY",
                    "status": "RESERVED",
                    "operation": OPERATION,
                    "product_id": product_id,
                    "destination_id": destination_id,
                    "history_id": history_id,
                    "origin": product.original_url,
                    "payload_sha256": self.generator.payload_sha256(product.original_url),
                    "external_id": product.external_id,
                    "variant_id": product.variant_id,
                    "attempts": 0,
                    "created_at": self.clock().isoformat(),
                    "product_identity_verified": False,
                    "account_verified": False,
                }
                inserted = session.execute(
                    conflict_insert(session, AppSetting)
                    .values(
                        key=key,
                        value=value,
                        description="Offline affiliate reservation; never retry.",
                    )
                    .on_conflict_do_nothing(index_elements=[AppSetting.key])
                    .returning(AppSetting.id)
                ).scalar_one_or_none()
                if inserted is None:
                    session.rollback()
                    return self.read(key) | {"reused": True}
                self._audit(session, key, value, "RESERVED")
                session.commit()
            with self.factory() as session:
                if not renew_lease(session, lease, owner, lease_seconds, self.clock):
                    raise IntegrationError("LEASE_LOST", "Offline affiliate reservation expired.")
                row = self._row(session, key)
                row.value = {
                    **row.value,
                    "status": "CLAIMED",
                    "attempts": 1,
                    "claimed_at": self.clock().isoformat(),
                }
                origin = row.value["origin"]
                self._audit(session, key, row.value, "CLAIMED")
                session.commit()  # Durable claim always precedes the mock HTTP.
            self.require_offline()
            result = self.generator.generate(origin)
            self.require_offline()
            if (
                not isinstance(result, AffiliateLinkResult)
                or result.product_identity_verified is not True
                or not _link_target_verified(result.long_url, identity)
            ):
                raise InvalidPayload(
                    "Returned link does not prove the exact product/variant identity."
                )
            validate_affiliate_url(Platform.SHOPEE, result.url)
            with self.factory() as session:
                if not renew_lease(session, lease, owner, lease_seconds, self.clock):
                    raise IntegrationError(
                        "LEASE_LOST", "Offline affiliate result lost its reservation."
                    )
                product, _ = self._eligible(session, product_id, destination_id, history_id)
                if self._key(product, destination_id) != key:
                    raise InvalidPayload("Product identity changed during the mock operation.")
                self._save_result(session, key, product, result)
                session.commit()
            return self.read(key) | {"reused": False}
        except BaseException as error:
            # Persist interruption as UNCERTAIN before propagating cancellation.
            # HTTP 5xx, transport failures, interruptions and persistence failures
            # cannot establish whether a mutation was consumed.
            code = error.code if isinstance(error, IntegrationError) else "PERSISTENCE_OR_AMBIGUOUS"
            known_failure = code in {
                "AUTHORIZATION_DENIED",
                "UPSTREAM_GRAPHQL_ERROR",
                "INVALID_PAYLOAD",
            }
            with self.factory() as session:
                row = self._row(session, key)
                if row is not None:
                    status = "FAILED" if known_failure else "UNCERTAIN"
                    # Never store upstream text or an exception containing URLs.
                    safe_code = code if known_failure else "UNCERTAIN_RESULT"
                    row.value = {
                        **row.value,
                        "status": status,
                        "error_code": safe_code,
                        "finished_at": self.clock().isoformat(),
                    }
                    self._audit(session, key, row.value, status, safe_code)
                    session.commit()
            if not isinstance(error, Exception):
                raise
            return self.read(key) | {"reused": False}
        finally:
            release_lease(self.factory, lease, owner)

    def _save_result(self, session, key, product, result):
        row = self._row(session, key)
        associated = result.account_verified is True and product.affiliate_link is None
        if associated:
            session.add(
                AffiliateLink(product_id=product.id, url=result.url, source="SHOPEE_OFFICIAL_API")
            )
        row.value = {
            **row.value,
            "status": "ASSOCIATED" if associated else "GENERATED",
            "finished_at": self.clock().isoformat(),
            "product_identity_verified": True,
            "account_verified": result.account_verified is True,
            "attribution_evidence": {
                "mode": "MOCK_ONLY",
                "authenticated_generation": False,
                "mock_authenticated_generation": True,
                "product_identity": True,
                "credential_provenance": "SYNTHETIC_MOCK",
                "ownership_attested": False,
                "conversion_recorded": False,
                "commission_validated": False,
            },
            "short_url": result.url,
            "long_url": result.long_url,
            "result_fingerprint": hashlib.sha256(
                json.dumps([result.url, result.long_url], separators=(",", ":")).encode()
            ).hexdigest(),
        }
        self._audit(session, key, row.value, row.value["status"])


# Production uses a separate, exact, expressly approved batch. Existing pilots
# still authorize zero new links; neither their hashes nor the lab guards change.
REAL_LINK_PREFIX = "shopee_link_batch:"
OWNED_LINK_PREFIX = "shopee_owned_link:"


class CommercialLinkFinalizer:
    """Authorize links for confirmed commercial rows, then reuse official import.

    No invocation at construction, no retry, and no publication. A worker consumes
    resulting official-export offers through enqueue_documented_imports as usual.
    """

    def __init__(
        self,
        factory,
        settings,
        *,
        clock=None,
        transport=None,
        isolated_testing=False,
        daily_pilot_id=None,
    ):
        if (transport is not None) != isolated_testing or (
            isolated_testing and type(transport) is not httpx.MockTransport
        ):
            raise ValueError("Transporte de teste exige MockTransport e isolamento explícitos.")
        self.factory, self.settings = factory, settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.transport, self.isolated_testing = transport, isolated_testing
        self.daily_pilot_id = daily_pilot_id

    def _sign(self, value):
        return hmac.new(
            self.settings.shopee_secret.get_secret_value().encode(),
            ("commercial-link:" + fingerprint(value)).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _safe(self, session):
        if self.isolated_testing:
            require_simulation_scope(session, self.settings)
        else:
            from sqlalchemy import text

            if session.get_bind().dialect.name != "postgresql" or session.execute(
                text("SELECT current_database(),current_schema()")
            ).one() != ("ofertas_bot", "public"):
                raise ValueError("Geração real exige banco operacional conhecido.")
            monitoring = session.scalar(
                select(AppSetting.value).where(AppSetting.key == "monitoring_config")
            )
            if not monitoring or monitoring.get("scheduler_enabled") is not False:
                raise ValueError("Scheduler legado deve permanecer desligado.")

    def _load(self, session, identifier, *, lock=False):
        self._safe(session)
        query = select(AppSetting).where(
            AppSetting.key == REAL_LINK_PREFIX + str(UUID(str(identifier)))
        )
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise ValueError("Autorização de links inexistente.")
        value = dict(row.value)
        scope = value["scope"]
        from app.services.shopee_evidence import credential_binding

        if (
            scope["credential_fingerprint"] != credential_binding(self.settings)
            or scope["environment"] != ("ISOLATED_MOCK" if self.isolated_testing else "PRODUCTION")
            or value["scope_sha256"] != fingerprint(scope)
            or not hmac.compare_digest(value.get("scope_signature", ""), self._sign(scope))
        ):
            raise ValueError("Escopo ou credencial divergente.")
        return row, value

    def _candidates(self, session, export):
        from app.services.official_offer_import import OfficialExportInput, _document, _row
        from app.services.shopee_evidence import account_evidence

        data = OfficialExportInput.model_validate(export)
        if not (
            data.confirmed_official_document
            and data.confirmed_column_semantics
            and data.confirmed_commercial_import
        ):
            raise ValueError("Documento comercial real e autorização de cadastro obrigatórios.")
        proof = account_evidence(session, self.settings, now=self.clock())
        if proof is None or data.account_reference != proof["account_reference"]:
            raise ValueError("Prova vigente de conta/credencial e conta do documento obrigatórias.")
        rows, _ = _document(data, self.clock())
        if len(rows) > 10:
            raise ValueError("Lote limitado a dez anúncios documentados.")
        candidates = []
        for line in rows:
            product, identity, price, observed = _row(
                session, data, line, self.clock(), require_link=False
            )
            if any(c["product_id"] == product.id for c in candidates):
                raise ValueError("Anúncio/opção repetido no lote.")
            # Keep an existing opaque link intact. Legacy links must be handled
            # through their documentary readiness, never overwritten here.
            if product.affiliate_link is not None:
                raise ValueError("Preserve o link existente; geração adicional desnecessária.")
            origin = "https://shopee.com.br/product/" + product.external_id.replace(":", "/")
            candidates.append(
                dict(
                    product_id=product.id,
                    identity=product.external_id,
                    variant_id=product.variant_id,
                    origin_url=origin,
                    public_price=str(price),
                    observed_at=observed.isoformat(),
                    identity_document=identity.model_dump(mode="json"),
                    payload_sha256=MockShopeeLinkGenerator.payload_sha256(origin),
                )
            )
        return data, proof, candidates

    def prepare(self, identifier, export, *, actor, not_before, expires_at):
        from app.services.comparison import aware_utc

        if not_before.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("Janela exige fuso horário.")
        not_before, expires_at = aware_utc(not_before), aware_utc(expires_at)
        if not not_before < expires_at or expires_at <= self.clock():
            raise ValueError("Janela inválida.")
        with self.factory() as session:
            self._safe(session)
            data, proof, candidates = self._candidates(session, export)
            if any(
                expires_at > datetime.fromisoformat(c["observed_at"]) + timedelta(seconds=600)
                for c in candidates
            ):
                raise ValueError(
                    "A janela não pode ultrapassar a validade comercial de 600 segundos."
                )
            scope = dict(
                authorization_id=str(UUID(str(identifier))),
                actor=actor,
                operation=OPERATION,
                endpoint=ENDPOINT,
                not_before=not_before.isoformat(),
                expires_at=expires_at.isoformat(),
                max_posts=len(candidates),
                retry=False,
                account_evidence_id=proof["id"],
                credential_fingerprint=proof["credential_fingerprint"],
                environment="ISOLATED_MOCK" if self.isolated_testing else "PRODUCTION",
                candidates=candidates,
                export=data.model_dump(mode="json"),
                publication_authorized=False,
            )
            key = REAL_LINK_PREFIX + scope["authorization_id"]
            if session.scalar(select(AppSetting.id).where(AppSetting.key == key)):
                raise ValueError("UUID já utilizado; não reutilizar.")
            value = dict(
                scope=scope,
                scope_sha256=fingerprint(scope),
                scope_signature=self._sign(scope),
                state="PREPARED",
                approval=None,
                requests_reserved=0,
                receipts=[],
            )
            session.add(AppSetting(key=key, value=value))
            session.commit()
        return self.report(identifier)

    def report(self, identifier):
        with self.factory() as session:
            _, value = self._load(session, identifier)
            return {
                k: v for k, v in value.items() if k not in {"scope_signature", "approval_signature"}
            }

    def _listing_link_candidates(self, session, product_ids):
        """Link identity is independent of price/stock; never verifies an offer."""
        from app.services.shopee_evidence import account_evidence
        from app.services.shopee_identity import expected_identity, url_identity

        proof = account_evidence(session, self.settings, now=self.clock())
        if (
            proof is None
            or not 1 <= len(product_ids) <= 3
            or len(set(product_ids)) != len(product_ids)
        ):
            raise ValueError("Uma a três identidades e prova vigente de conta obrigatórias.")
        candidates = []
        for product_id in product_ids:
            product = session.get(Product, product_id)
            if (
                product is None
                or product.platform != Platform.SHOPEE
                or product.variant_id
                or url_identity(product.original_url) != expected_identity(product)
                or product.affiliate_link is not None
            ):
                raise ValueError("Anúncio/URL inválido ou link existente; não substituir.")
            origin = "https://shopee.com.br/product/" + product.external_id.replace(":", "/")
            candidates.append(
                dict(
                    product_id=product.id,
                    identity=product.external_id,
                    variant_id="",
                    origin_url=origin,
                    product_binding_sha256=fingerprint(
                        {
                            "identity": product.external_id,
                            "variant_id": product.variant_id,
                            "original_url": product.original_url,
                            "name": product.name,
                        }
                    ),
                    payload_sha256=MockShopeeLinkGenerator.payload_sha256(origin),
                )
            )
        return proof, candidates

    def prepare_listing_links(
        self, identifier, product_ids, *, actor, not_before, expires_at, daily_pilot_id=None
    ):
        """Separate signed preflight batch for ANNOUNCEMENT_ONLY; no HTTP on prepare."""
        from app.services.comparison import aware_utc

        not_before, expires_at = aware_utc(not_before), aware_utc(expires_at)
        if not 0 < (expires_at - not_before).total_seconds() <= 7200 or expires_at <= self.clock():
            raise ValueError("Janela de links inválida.")
        with self.factory() as session:
            self._safe(session)
            proof, candidates = self._listing_link_candidates(session, product_ids)
            scope = dict(
                authorization_id=str(UUID(str(identifier))),
                actor=actor,
                purpose="ANNOUNCEMENT_ONLY_LINKS",
                operation=OPERATION,
                endpoint=ENDPOINT,
                not_before=not_before.isoformat(),
                expires_at=expires_at.isoformat(),
                max_posts=len(candidates),
                retry=False,
                fallback=False,
                account_evidence_id=proof["id"],
                credential_fingerprint=proof["credential_fingerprint"],
                environment="ISOLATED_MOCK" if self.isolated_testing else "PRODUCTION",
                candidates=candidates,
                publication_authorized=False,
                commercial_verification=False,
            )
            key = REAL_LINK_PREFIX + scope["authorization_id"]
            if daily_pilot_id is not None:
                if str(daily_pilot_id) != self.daily_pilot_id:
                    raise ValueError("Lote diário exige o executor vinculado.")
                scope["daily_pilot_id"] = str(UUID(str(daily_pilot_id)))
                from app.services.daily_supply import require_daily_link

                require_daily_link(session, self.settings, scope, self.clock())
            if session.scalar(select(AppSetting.id).where(AppSetting.key == key)):
                raise ValueError("UUID já utilizado.")
            session.add(
                AppSetting(
                    key=key,
                    value=dict(
                        scope=scope,
                        scope_sha256=fingerprint(scope),
                        scope_signature=self._sign(scope),
                        state="PREPARED",
                        approval=None,
                        requests_reserved=0,
                        receipts=[],
                    ),
                )
            )
            session.commit()
        return self.report(identifier)

    def _refresh_candidates(self, session, value):
        if value["scope"].get("purpose") == "ANNOUNCEMENT_ONLY_LINKS":
            return self._listing_link_candidates(
                session, [c["product_id"] for c in value["scope"]["candidates"]]
            )[1]
        return self._candidates(session, value["scope"]["export"])[2]

    def approve(self, identifier, *, scope_sha256, actor, confirmed_link_generation):
        if confirmed_link_generation is not True or not actor:
            raise ValueError(
                "Autorização explícita para geração e cadastro deste lote obrigatória."
            )
        with self.factory() as session:
            row, value = self._load(session, identifier, lock=True)
            if value["scope"].get("daily_pilot_id") != self.daily_pilot_id:
                raise ValueError("Lote diário exige o executor vinculado.")
            if value["state"] != "PREPARED" or scope_sha256 != value["scope_sha256"]:
                raise ValueError("Escopo deve estar PREPARED e íntegro.")
            if self._refresh_candidates(session, value) != value["scope"]["candidates"]:
                raise ValueError("Identidade alterada após preparação.")
            approval = dict(
                scope_sha256=scope_sha256,
                actor=actor,
                at=self.clock().isoformat(),
                confirmed_link_generation=True,
            )
            value.update(
                state="APPROVED", approval=approval, approval_signature=self._sign(approval)
            )
            row.value = value
            session.commit()
        return self.report(identifier)

    def _authorized(self, session, value):
        from app.services.shopee_evidence import account_evidence

        scope = value["scope"]
        if scope.get("daily_pilot_id") != self.daily_pilot_id:
            raise ValueError("Lote diário exige o executor vinculado.")
        approval = value.get("approval")
        if (
            not approval
            or approval.get("scope_sha256") != value["scope_sha256"]
            or not hmac.compare_digest(value.get("approval_signature", ""), self._sign(approval))
            or not datetime.fromisoformat(scope["not_before"])
            <= self.clock()
            < datetime.fromisoformat(scope["expires_at"])
        ):
            raise ValueError("Aprovação ausente, inválida ou fora da janela.")
        proof = account_evidence(
            session, self.settings, evidence_id=scope["account_evidence_id"], now=self.clock()
        )
        if proof is None:
            raise ValueError("Prova da conta venceu ou foi invalidada.")
        if scope.get("daily_pilot_id"):
            from app.services.daily_supply import require_daily_link

            return require_daily_link(session, self.settings, scope, self.clock())
        return None

    def execute(self, identifier):
        execution_token = uuid4().hex
        try:
            return self._execute(identifier, execution_token)
        except Exception:
            with self.factory() as session:
                row, value = self._load(session, identifier, lock=True)
                if value["state"] != "CLAIMED" or value.get("execution_token") != execution_token:
                    raise
                value.update(state="UNCERTAIN", error_code="RECONCILIATION_REQUIRED_NO_RETRY")
                row.value = value
                session.commit()
            return self.report(identifier)

    def _execute(self, identifier, execution_token):
        # Claim the entire batch once, before creating any HTTP client. A crash
        # leaves CLAIMED/reservations for reconciliation, never automatic replay.
        with self.factory() as session:
            row, value = self._load(session, identifier, lock=True)
            self._authorized(session, value)
            if value["state"] != "APPROVED" or value["requests_reserved"]:
                raise ValueError("Lote consumido ou indisponível; não repetir.")
            candidates = self._refresh_candidates(session, value)
            if candidates != value["scope"]["candidates"]:
                raise ValueError("Candidato ou evidência alterados após aprovação.")
            value["state"] = "CLAIMED"
            value["execution_token"] = execution_token
            row.value = value
            session.commit()
        for index, candidate in enumerate(candidates):
            with self.factory() as session:
                row, value = self._load(session, identifier, lock=True)
                self._authorized(session, value)
                if value["state"] != "CLAIMED" or value["requests_reserved"] != index:
                    raise ValueError("Reserva divergente.")
                # A separate durable identity claim blocks races across UUIDs.
                claim_key = "shopee_link_claim:" + fingerprint(
                    {"identity": candidate["identity"], "variant": candidate["variant_id"]}
                )
                inserted = session.execute(
                    conflict_insert(session, AppSetting)
                    .values(key=claim_key, value={"authorization_id": str(identifier)})
                    .on_conflict_do_nothing(index_elements=[AppSetting.key])
                    .returning(AppSetting.id)
                )
                if inserted.scalar_one_or_none() is None:
                    raise ValueError("Anúncio já reservado por outra operação; não repetir.")
                value["requests_reserved"] += 1
                row.value = value
                session.commit()

            used = [False]
            paced = [False]

            def preserve_response(response, candidate=candidate, scope=value["scope"], paced=paced):
                """Whitelist provider link fields before parsing; never headers/errors."""
                if paced[0] and response.status_code == 429:
                    from app.services.daily_queue import defer_shopee

                    defer_shopee(self.factory, self.settings, response, self.clock())
                body = response.read()
                observation = dict(
                    http_status=response.status_code,
                    response_sha256=hashlib.sha256(body).hexdigest(),
                    received_at=self.clock().isoformat(),
                    product_id=candidate["product_id"],
                    credential_fingerprint=scope["credential_fingerprint"],
                    account_evidence_id=scope["account_evidence_id"],
                    payload_sha256=candidate["payload_sha256"],
                    verification_status="UNVERIFIED_PROVIDER_RESPONSE",
                )
                try:
                    decoded = json.loads(body) if len(body) <= 2_000_000 else None
                except (ValueError, UnicodeError):
                    decoded = None
                observation["json_object"] = isinstance(decoded, dict)
                data = decoded.get("data") if isinstance(decoded, dict) else None
                links = data.get("generateShortLink") if isinstance(data, dict) else None
                observation["link_result_type"] = type(links).__name__
                # A malformed response may still contain a generated short link.
                # Preserve it for reconciliation, without declaring it usable.
                for field in ("shortLink", "longLink"):
                    raw = links.get(field) if isinstance(links, dict) else None
                    observation[field + "_type"] = type(raw).__name__
                    if isinstance(raw, str):
                        observation[field + "_length"] = len(raw)
                        try:
                            from urllib.parse import parse_qsl

                            from app.services.message_templates import validate_public_url

                            validate_public_url(raw)
                            parts = urlsplit(raw)
                            if parts.hostname not in {
                                "shopee.com.br",
                                "www.shopee.com.br",
                                "s.shopee.com.br",
                                "shope.ee",
                            }:
                                continue
                            if any(
                                any(
                                    term in key.casefold()
                                    for term in (
                                        "secret",
                                        "password",
                                        "token",
                                        "authorization",
                                        "signature",
                                    )
                                )
                                for key, _ in parse_qsl(parts.query)
                            ):
                                continue
                            observation[field] = raw
                        except (ValueError, TypeError):
                            pass
                with self.factory() as session:
                    row, current = self._load(session, identifier, lock=True)
                    if (
                        current.get("execution_token") != execution_token
                        or current["state"] != "CLAIMED"
                    ):
                        raise ValueError("Resposta não corresponde à operação em execução.")
                    current["provider_http_observations"] = [
                        *current.get("provider_http_observations", []),
                        observation,
                    ]
                    row.value = current
                    session.commit()

            def guard(request, candidate=candidate, index=index, used=used, paced=paced):
                with self.factory() as session:
                    _, current = self._load(session, identifier)
                    policy = self._authorized(session, current)
                    self._current_candidate(session, current, index)
                    if (
                        used[0]
                        or current["state"] != "CLAIMED"
                        or current["requests_reserved"] != index + 1
                        or request.method != "POST"
                        or str(request.url) != ENDPOINT
                        or hashlib.sha256(request.content).hexdigest()
                        != candidate["payload_sha256"]
                    ):
                        raise ValueError("HTTP fora da reserva exata.")
                    if policy and policy.shopee_posts_per_minute:
                        from app.services.daily_queue import shopee_ready

                        if not shopee_ready(
                            session,
                            self.settings,
                            policy.shopee_posts_per_minute,
                            self.clock(),
                            reserve=True,
                        ):
                            raise ValueError("Ritmo Shopee temporariamente bloqueado.")
                        session.commit()
                        paced[0] = True
                used[0] = True

            with httpx.Client(
                transport=self.transport or httpx.HTTPTransport(retries=0),
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                event_hooks={"request": [guard], "response": [preserve_response]},
            ) as client:
                connector = ShopeeConnector(
                    JsonTransport(client=client, max_retries=0, cache_seconds=0, min_interval=0),
                    app_id=self.settings.shopee_app_id,
                    secret=self.settings.shopee_secret,
                    enabled=True,
                    short_links_enabled=True,
                    clock=self.clock,
                )
                try:
                    result = connector.generate_affiliate_link(candidate["origin_url"])
                except Exception as error:
                    with self.factory() as session:
                        row, value = self._load(session, identifier, lock=True)
                        # Any transport/parser uncertainty consumes the reservation.
                        value.update(
                            state="UNCERTAIN",
                            error_code=getattr(error, "code", "UNCONFIRMED_RESULT"),
                        )
                        row.value = value
                        session.commit()
                    return self.report(identifier)
            # Preserve the confirmed provider response before revalidating the
            # now-current commercial deadline. Expiry after HTTP must not erase
            # provenance or cause another generation attempt.
            with self.factory() as session:
                row, value = self._load(session, identifier, lock=True)
                value["provider_results"] = [
                    *value.get("provider_results", []),
                    dict(
                        product_id=candidate["product_id"],
                        identity=candidate["identity"],
                        origin_url=candidate["origin_url"],
                        short_link=result.url,
                        long_link=result.long_url,
                        credential_fingerprint=value["scope"]["credential_fingerprint"],
                        account_evidence_id=value["scope"]["account_evidence_id"],
                        payload_sha256=candidate["payload_sha256"],
                        received_at=self.clock().isoformat(),
                        association_status="PENDING_REVALIDATION",
                        operationally_verified=False,
                    ),
                ]
                row.value = value
                session.commit()
            with self.factory() as session:
                row, value = self._load(session, identifier, lock=True)
                self._authorized(session, value)
                product = session.scalar(
                    select(Product).where(Product.id == candidate["product_id"]).with_for_update()
                )
                self._current_candidate(session, value, index)
                if product.affiliate_link is not None:
                    raise ValueError(
                        "Link foi associado por outra operação; resultado exige conciliação."
                    )
                # Account proof + exact authenticated generation establishes this
                # association; generation success alone still never suffices.
                receipt = dict(
                    authorization_id=str(identifier),
                    product_id=product.id,
                    identity=product.external_id,
                    variant_id=product.variant_id,
                    original_url_sha256=fingerprint({"original_url": product.original_url}),
                    origin_url=candidate["origin_url"],
                    short_link=result.url,
                    long_link=result.long_url,
                    product_identity_verified=result.product_identity_verified,
                    credential_fingerprint=value["scope"]["credential_fingerprint"],
                    account_evidence_id=value["scope"]["account_evidence_id"],
                    payload_sha256=candidate["payload_sha256"],
                    observed_at=self.clock().isoformat(),
                    link_selects_variant=False,
                    variant_selection_required=bool(product.variant_id),
                )
                if not result.product_identity_verified:
                    raise ValueError("Identidade do link não comprovada.")
                receipt["signature"] = self._sign(receipt)
                session.add(AppSetting(key=OWNED_LINK_PREFIX + str(product.id), value=receipt))
                session.add(
                    AffiliateLink(
                        product_id=product.id, url=result.url, source="SHOPEE_OFFICIAL_API"
                    )
                )
                value["receipts"] = [*value["receipts"], receipt]
                row.value = value
                session.commit()
        from app.services.official_offer_import import process_export

        with self.factory() as session:
            row, value = self._load(session, identifier, lock=True)
            self._authorized(session, value)
            imported = (
                None
                if value["scope"].get("purpose") == "ANNOUNCEMENT_ONLY_LINKS"
                else process_export(
                    session, self.settings, value["scope"]["export"], persist=True, now=self.clock()
                )
            )
            value.update(state="SUCCEEDED", commercial_import=imported)
            row.value = value
            session.commit()
        return self.report(identifier)

    def _current_candidate(self, session, value, index):
        if value["scope"].get("purpose") == "ANNOUNCEMENT_ONLY_LINKS":
            expected = value["scope"]["candidates"][index]
            current = self._listing_link_candidates(session, [expected["product_id"]])[1][0]
            if current != expected:
                raise ValueError("Identidade/URL alterada após preparação.")
            return
        from app.services.official_offer_import import OfficialExportInput, _document, _row

        data = OfficialExportInput.model_validate(value["scope"]["export"])
        rows, _ = _document(data, self.clock())
        product, identity, price, observed = _row(
            session, data, rows[index], self.clock(), require_link=False
        )
        expected = value["scope"]["candidates"][index]
        if (
            product.id != expected["product_id"]
            or product.variant_id != expected["variant_id"]
            or identity.model_dump(mode="json") != expected["identity_document"]
            or str(price) != expected["public_price"]
            or observed.isoformat() != expected["observed_at"]
        ):
            raise ValueError("Identidade ou comprovação comercial alterada.")


def validate_owned_link_receipt(session, settings, product, now):
    """Revalidate future generated links at offer consumption, including expiry."""
    from app.services.shopee_evidence import account_evidence, credential_binding

    value = session.scalar(
        select(AppSetting.value).where(AppSetting.key == OWNED_LINK_PREFIX + str(product.id))
    )
    if value is None:
        return  # Existing manual/legacy association gates are unchanged.
    receipt = dict(value)
    signature = receipt.pop("signature", "")
    expected = hmac.new(
        settings.shopee_secret.get_secret_value().encode(),
        ("commercial-link:" + fingerprint(receipt)).encode(),
        hashlib.sha256,
    ).hexdigest()
    if (
        not hmac.compare_digest(signature, expected)
        or receipt["credential_fingerprint"] != credential_binding(settings)
        or receipt["identity"] != product.external_id
        or receipt["variant_id"] != product.variant_id
        or receipt["original_url_sha256"] != fingerprint({"original_url": product.original_url})
        or product.affiliate_link is None
        or receipt["short_link"] != product.affiliate_link.url
        or account_evidence(session, settings, evidence_id=receipt["account_evidence_id"], now=now)
        is None
    ):
        raise ValueError("Proveniência do link próprio invalidada; não publicar.")

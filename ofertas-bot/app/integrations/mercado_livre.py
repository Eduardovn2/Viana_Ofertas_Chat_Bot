"""Read-only Mercado Livre API connector, disabled unless explicitly configured.

The contract is based on docs/INTEGRACOES_FASE_3.md. Listing metadata is never
used as a source for the deprecated price/base_price/original_price fields.
"""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlsplit

from pydantic import SecretStr

from app.integrations.contracts import (
    Capability,
    CapabilityStatus,
    ProductRef,
    ProductSnapshot,
    fingerprint,
)
from app.integrations.errors import ConnectorDisabled, InvalidPayload, UnsupportedCapability
from app.integrations.http import JsonResponse, JsonTransport
from app.models import Availability, Platform, PriceAccess, SourceType
from app.services.products import normalize_product_identity

_API = "https://api.mercadolibre.com"
_SOURCE = "mercado_livre:official_api"
_CONTEXT = "channel_marketplace"
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "cookie",
        "secret",
        "token",
    }
)


def _money(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise InvalidPayload("Valor monetário incompatível com o contrato oficial.")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not Decimal("0") < amount <= Decimal("9999999999.99"):
            raise InvalidPayload("Preço oficial deve ser finito e maior que zero.")
        cents = amount.quantize(Decimal("0.01"))
        if amount != cents:
            raise InvalidPayload("Preço oficial contém frações de centavo não suportadas.")
    except InvalidOperation:
        raise InvalidPayload("Valor monetário incompatível com o contrato oficial.") from None
    return cents


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidPayload("O instante da resposta deve incluir fuso horário.")
    return value.astimezone(UTC)


def _date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except (ValueError, InvalidPayload):
        return None


def _identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    normalized = str(value)
    return normalized if re.fullmatch(r"[0-9]{1,100}", normalized) else None


def _safe_product_url(url: str, item_id: str) -> str:
    try:
        if normalize_product_identity(Platform.MERCADO_LIVRE, url, item_id) != item_id:
            raise ValueError
        parsed = urlsplit(url)
        if any(
            key.casefold() in _SENSITIVE_QUERY_KEYS
            for key, _ in [*parse_qsl(parsed.query), *parse_qsl(parsed.fragment)]
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise InvalidPayload(
            "A URL canônica não corresponde a um produto brasileiro autorizado."
        ) from None
    return url


def _variant_context(url: str) -> bool:
    parsed = urlsplit(url)
    selectors = {
        "variation",
        "variation_id",
        "searchvariation",
        "attributes",
        "attribute_combinations",
    }
    return any(
        key.casefold() in selectors
        for key, _ in [*parse_qsl(parsed.query), *parse_qsl(parsed.fragment)]
    )


def _availability(data: dict) -> Availability:
    status = data.get("status")
    if status is not None and not isinstance(status, str):
        raise InvalidPayload("Estado do item incompatível com o contrato oficial.")
    if status in {"paused", "closed", "deleted", "inactive", "under_review"}:
        return Availability.UNAVAILABLE
    quantity = data.get("available_quantity")
    if quantity is not None and (type(quantity) is not int or quantity < 0):
        raise InvalidPayload("Quantidade referencial incompatível com o contrato oficial.")
    if status != "active" or quantity is None:
        return Availability.UNKNOWN
    return Availability.AVAILABLE if quantity > 0 else Availability.UNAVAILABLE


def _image(data: dict) -> str | None:
    pictures = data.get("pictures")
    candidates = (
        [picture.get("secure_url") for picture in pictures if isinstance(picture, dict)]
        if isinstance(pictures, list)
        else []
    )
    candidates.append(data.get("secure_thumbnail"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        try:
            parsed = urlsplit(candidate)
            if (
                parsed.scheme == "https"
                and parsed.hostname in {"http2.mlstatic.com", "http.mlstatic.com"}
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and "\\" not in candidate
                and not any(character.isspace() for character in candidate)
            ):
                return candidate
        except ValueError:
            continue
    return None


class MercadoLivreConnector:
    platform = Platform.MERCADO_LIVRE

    def __init__(
        self,
        transport: JsonTransport,
        token: SecretStr | None = None,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ):
        self.transport = transport
        self.token = token
        self.enabled = enabled
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def credentials_configured(self) -> bool:
        return isinstance(self.token, SecretStr) and bool(
            re.fullmatch(r"[A-Za-z0-9._~-]+", self.token.get_secret_value())
        )

    def capabilities(self) -> list[Capability]:
        documented = (
            "API oficial documentada; exige token e permissão da conta, ainda não comprovada."
        )
        return [
            Capability("fetch_product", CapabilityStatus.REQUIRES_CREDENTIALS, documented),
            Capability("public_price", CapabilityStatus.REQUIRES_CREDENTIALS, documented),
            Capability("price_conditions", CapabilityStatus.REQUIRES_CREDENTIALS, documented),
            Capability(
                "discover_offers",
                CapabilityStatus.UNVERIFIED,
                "Busca ampla para a conta afiliada não verificada.",
            ),
            Capability(
                "fetch_coupons",
                CapabilityStatus.UNVERIFIED,
                "Campanhas de vendedor não comprovam cupons de terceiros.",
            ),
            Capability(
                "manual_affiliate_link",
                CapabilityStatus.SUPPORTED,
                "Cadastro manual de link próprio gerado no Link Builder oficial.",
            ),
            Capability(
                "generate_affiliate_link",
                CapabilityStatus.UNVERIFIED,
                "Conversão automática não documentada para esta conta.",
            ),
            Capability(
                "variant_price",
                CapabilityStatus.UNVERIFIED,
                "Preço atual de variante não resolvida não é inferido.",
            ),
        ]

    def discover_offers(self, query: str, **kwargs):
        raise UnsupportedCapability(
            "Descoberta automática do Mercado Livre não está autorizada neste conector."
        )

    def fetch_coupons(self, ref: ProductRef):
        raise UnsupportedCapability(
            "Consulta de cupons de terceiros do Mercado Livre não foi verificada."
        )

    def generate_affiliate_link(self, url: str):
        raise UnsupportedCapability(
            "Use o Link Builder oficial e cadastre manualmente seu link próprio."
        )

    def _request(self, path: str) -> JsonResponse:
        secret = self.token.get_secret_value()
        namespace = hashlib.sha256(secret.encode()).hexdigest()[:20]
        response = self.transport.request(
            "GET",
            f"{_API}{path}",
            headers={"Authorization": f"Bearer {secret}"},
            cache_key=f"mercado_livre:{namespace}:{path}",
        )
        if not isinstance(response.data, dict):
            raise InvalidPayload()
        fetched_at = _utc(response.fetched_at)
        if fetched_at > _utc(self.clock()) + timedelta(seconds=60):
            raise InvalidPayload("O instante de consulta oficial está no futuro.")
        return JsonResponse(response.data, fetched_at)

    def _price_access(
        self, sale: dict, prices: dict, item_id: str, current: Decimal, now: datetime
    ) -> tuple[PriceAccess, str]:
        if prices.get("id") != item_id or not isinstance(prices.get("prices"), list):
            raise InvalidPayload("A lista de preços não corresponde ao item solicitado.")
        if any(not isinstance(row, dict) for row in prices["prices"]):
            raise InvalidPayload("A lista oficial de preços é incompatível com o contrato.")
        price_id = _identifier(sale.get("price_id"))
        if price_id is None:
            return PriceAccess.UNKNOWN, "price_id_missing"
        matches = [row for row in prices["prices"] if _identifier(row.get("id")) == price_id]
        if not matches:
            return PriceAccess.UNKNOWN, "price_id_unmatched"
        if len(matches) != 1:
            raise InvalidPayload("A lista oficial contém identificadores de preço ambíguos.")
        row = matches[0]
        if row.get("currency_id") != "BRL" or _money(row.get("amount")) != current:
            raise InvalidPayload("As respostas oficiais divergem sobre preço ou moeda.")
        conditions = row.get("conditions")
        if (
            not isinstance(conditions, dict)
            or not {"context_restrictions", "start_time", "end_time"} <= conditions.keys()
        ):
            return PriceAccess.UNKNOWN, "conditions_hidden_or_missing"
        if conditions.keys() - {
            "context_restrictions",
            "start_time",
            "end_time",
            "min_purchase_unit",
            "eligible",
        }:
            return PriceAccess.UNKNOWN, "unknown_price_condition"
        restrictions = conditions["context_restrictions"]
        if not isinstance(restrictions, list) or any(
            not isinstance(value, str) for value in restrictions
        ):
            return PriceAccess.UNKNOWN, "conditions_invalid"
        start = _date(conditions["start_time"]) if conditions["start_time"] is not None else None
        end = _date(conditions["end_time"]) if conditions["end_time"] is not None else None
        if (
            (conditions["start_time"] is not None and start is None)
            or (conditions["end_time"] is not None and end is None)
            or (start is not None and start > now)
            or (end is not None and end <= now)
            or (start is not None and end is not None and end <= start)
        ):
            return PriceAccess.UNKNOWN, "price_validity_unverified"
        if any(value != _CONTEXT for value in restrictions):
            return PriceAccess.CONDITIONAL, "restricted_context"
        minimum = conditions.get("min_purchase_unit")
        if minimum is not None:
            if type(minimum) is not int or minimum < 1:
                return PriceAccess.UNKNOWN, "quantity_condition_unverified"
            if minimum > 1:
                return PriceAccess.CONDITIONAL, "minimum_quantity"
        if "eligible" in conditions and type(conditions["eligible"]) is not bool:
            return PriceAccess.UNKNOWN, "price_eligibility_unverified"
        if conditions.get("eligible") is False:
            return PriceAccess.UNKNOWN, "price_not_eligible"
        metadata = sale.get("metadata")
        if isinstance(metadata, dict) and metadata.get("is_price_per_quantity") is True:
            return PriceAccess.CONDITIONAL, "quantity_price"
        if row.get("type") not in {"standard", "promotion"}:
            return PriceAccess.UNKNOWN, "price_type_unverified"
        return PriceAccess.PUBLIC, "public_context_proven"

    def fetch_product(self, ref: ProductRef) -> ProductSnapshot:
        if not self.enabled or not self.credentials_configured:
            raise ConnectorDisabled(
                "Habilite o conector e configure um token autorizado do Mercado Livre."
            )
        if ref.platform != self.platform or not isinstance(ref.external_item_id, str):
            raise InvalidPayload("Referência de produto incompatível com o Mercado Livre.")
        match = re.fullmatch(r"MLB-?([0-9]{1,97})", ref.external_item_id, flags=re.IGNORECASE)
        if (
            match is None
            or not isinstance(ref.variant_id, str)
            or (ref.variant_id and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ref.variant_id) is None)
        ):
            raise InvalidPayload("Identificador de anúncio ou variante inválido.")
        item_id = f"MLB{match.group(1)}"
        canonical_url = _safe_product_url(ref.canonical_url, item_id)
        has_variant_context = bool(ref.variant_id) or _variant_context(canonical_url)
        response = self._request(f"/items/{item_id}")
        item = response.data
        if item.get("id") != item_id or item.get("site_id") != "MLB":
            raise InvalidPayload(
                "A resposta oficial não corresponde ao anúncio brasileiro solicitado."
            )
        name = item.get("title")
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise InvalidPayload("Nome do produto ausente ou incompatível com o contrato.")
        if item.get("permalink") is not None:
            returned_url = _safe_product_url(item["permalink"], item_id)
            if not has_variant_context:
                canonical_url = returned_url
        if item.get("currency_id") not in (None, "BRL"):
            raise InvalidPayload("A moeda do item não é BRL.")
        availability = _availability(item)
        variations = item.get("variations")
        if variations is not None and (
            not isinstance(variations, list)
            or any(not isinstance(value, dict) for value in variations)
        ):
            raise InvalidPayload("Variações incompatíveis com o contrato oficial.")
        unresolved = has_variant_context or variations is None or bool(variations)
        access, reason = (
            PriceAccess.UNKNOWN,
            "variant_unresolved" if unresolved else "item_unavailable",
        )
        current = previous = None
        dates = [response.fetched_at]
        price_id = None
        if not unresolved and availability != Availability.UNAVAILABLE:
            sale_response = self._request(f"/items/{item_id}/sale_price?context={_CONTEXT}")
            dates.append(sale_response.fetched_at)
            sale = sale_response.data
            if sale.get("currency_id") != "BRL":
                raise InvalidPayload("A moeda do preço oficial não é BRL.")
            current = _money(sale.get("amount"))
            price_id = _identifier(sale.get("price_id"))
            prices_response = self._request(f"/items/{item_id}/prices")
            dates.append(prices_response.fetched_at)
            access, reason = self._price_access(
                sale, prices_response.data, item_id, current, _utc(self.clock())
            )
            if access == PriceAccess.PUBLIC and sale.get("regular_amount") is not None:
                try:
                    old = _money(sale["regular_amount"])
                except InvalidPayload:
                    old = None
                previous = old if old is not None and old > current else None
        elif unresolved and availability != Availability.UNAVAILABLE:
            availability = Availability.UNKNOWN
        category_id = item.get("category_id")
        if category_id is not None and (
            not isinstance(category_id, str) or not re.fullmatch(r"MLB[0-9]{1,97}", category_id)
        ):
            raise InvalidPayload("Categoria oficial incompatível com o contrato.")
        seller_id = _identifier(item.get("seller_id"))
        image_url = _image(item)
        evidence = {
            "request_context": _CONTEXT,
            "price_access_reason": reason,
            "variant_resolved": not unresolved,
            "quantity_is_referential": True,
            "price_id": price_id,
        }
        normalized = {
            "platform": self.platform.value,
            "external_item_id": item_id,
            "variant_id": ref.variant_id,
            "canonical_url": canonical_url,
            "name": name.strip(),
            "currency": "BRL",
            "current_price": current,
            "previous_price": previous,
            "availability": availability.value,
            "price_access": access.value,
            "category_external_id": category_id,
            "seller_id": seller_id,
            "image_url": image_url,
            "evidence": evidence,
        }
        return ProductSnapshot(
            platform=self.platform,
            external_item_id=item_id,
            variant_id=ref.variant_id,
            canonical_url=canonical_url,
            name=name.strip(),
            category_external_id=category_id,
            category_external_name=None,
            image_url=image_url,
            seller_id=seller_id,
            current_price=current,
            previous_price=previous,
            currency="BRL",
            availability=availability,
            price_access=access,
            observed_at=min(dates),
            source=_SOURCE,
            is_simulated=False,
            source_type=SourceType.OFFICIAL_API,
            source_reference=f"{_API}/items/{item_id}/sale_price?context={_CONTEXT}"
            if current is not None
            else f"{_API}/items/{item_id}",
            raw_fingerprint=fingerprint(normalized),
            evidence_metadata=evidence,
        )

"""BR affiliate API adapter, based on the public Explorer V2 schema.

No network call occurs without explicit enablement and credentials. The API's
product offers do not establish stock, public pricing, or variant availability.
"""

import hashlib
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import uuid4

from pydantic import SecretStr

from app.integrations.contracts import (
    AffiliateLinkResult,
    Capability,
    CapabilityStatus,
    DiscoveryPage,
    ProductRef,
    ProductSnapshot,
    fingerprint,
)
from app.integrations.errors import (
    ConnectorDisabled,
    IntegrationError,
    InvalidPayload,
    UnsupportedCapability,
)
from app.integrations.http import JsonResponse, JsonTransport
from app.models import Availability, Platform, PriceAccess, SourceType
from app.services.products import normalize_product_identity, validate_affiliate_url

ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"
_PRODUCT_FIELDS = (
    "itemId shopId productName productLink imageUrl price priceMin priceMax "
    "productCatIds commissionRate commission"
)
_PAGE_FIELDS = "page limit hasNextPage"
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,18}\Z")
_REFERENCE_ID = re.compile(r"([1-9][0-9]{0,18}):([1-9][0-9]{0,18})\Z")
_PRODUCT_PATH = re.compile(r"/product/([1-9][0-9]{0,18})/([1-9][0-9]{0,18})/?\Z")
_SLUG_PATH = re.compile(r"/[^/]+-i\.([1-9][0-9]{0,18})\.([1-9][0-9]{0,18})/?\Z")


def _positive_id(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InvalidPayload("Identificador de produto Shopee inválido.")
    result = str(value)
    if not _NUMERIC_ID.fullmatch(result) or int(result) > 2**63 - 1:
        raise InvalidPayload("Identificador de produto Shopee inválido.")
    return result


def _url_identity(url: str) -> tuple[str, str]:
    try:
        normalize_product_identity(Platform.SHOPEE, url, None)
        parsed = urlsplit(url)
        if parsed.fragment:
            raise ValueError
        path = unquote(parsed.path)
        match = _PRODUCT_PATH.fullmatch(path) or _SLUG_PATH.fullmatch(path)
        if match is None:
            raise ValueError
        return _positive_id(match.group(1)), _positive_id(match.group(2))
    except (ValueError, TypeError):
        raise InvalidPayload(
            "Use uma URL canônica de produto Shopee com loja e item identificáveis."
        ) from None


def _category_id(value) -> str:
    result = _positive_id(value)
    if int(result) > 2**31 - 1:
        raise InvalidPayload("Identificador de categoria Shopee inválido.")
    return result


def _link_target_verified(long_url: str, expected: tuple[str, str]) -> bool:
    """Require the authenticated response to identify the requested product.

    An unfamiliar Shopee affiliate/deep-link format remains unverified. It is
    never followed or decoded speculatively to claim ownership of a product link.
    """
    try:
        actual = _url_identity(long_url)
    except InvalidPayload:
        try:
            normalize_product_identity(Platform.SHOPEE, long_url, None)
        except (ValueError, TypeError):
            raise InvalidPayload("O destino retornado não é uma URL Shopee permitida.") from None
        return False
    if actual != expected:
        raise InvalidPayload("O link retornado aponta para outro produto Shopee.")
    selectors = {
        "variation",
        "variation_id",
        "variationid",
        "searchvariation",
        "attributes",
        "modelid",
        "model_id",
        "sku",
        "sku_id",
    }
    return not any(key.casefold() in selectors for key, _ in parse_qsl(urlsplit(long_url).query))


def _money(value, *, optional: bool = True) -> Decimal | None:
    if value is None and optional:
        return None
    # The official schema exposes money as String, never as a binary float.
    if not isinstance(value, str) or len(value) > 40:
        raise InvalidPayload("Preço Shopee incompatível com o esquema oficial.")
    try:
        amount = Decimal(value)
        valid = (
            amount.is_finite()
            and 0 < amount <= Decimal("9999999999.99")
            and amount == amount.quantize(Decimal("0.01"))
        )
    except InvalidOperation:
        valid = False
    if not valid:
        raise InvalidPayload("Preço Shopee deve ser positivo, finito e representável em centavos.")
    return amount.quantize(Decimal("0.01"))


def _commission(value) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return str(amount) if amount.is_finite() and amount >= 0 else None


def _image(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        valid = (
            parsed.scheme == "https"
            and (host == "cf.shopee.com.br" or host.endswith(".susercontent.com"))
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in value)
            and "\\" not in value
        )
    except ValueError:
        valid = False
    return value if valid else None


class ShopeeConnector:
    platform = Platform.SHOPEE

    def __init__(
        self,
        transport: JsonTransport,
        app_id: SecretStr | None = None,
        secret: SecretStr | None = None,
        enabled: bool = False,
        short_links_enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ):
        self.transport = transport
        self.app_id = app_id
        self.secret = secret
        self.enabled = enabled
        self.short_links_enabled = short_links_enabled
        self.clock = clock or (lambda: datetime.now(UTC))
        # Distinct instances/accounts cannot share cached authenticated evidence accidentally.
        self._cache_scope = uuid4().hex
        self._verified_operations: set[str] = set()

    @property
    def credentials_configured(self) -> bool:
        return bool(
            self.app_id
            and self.secret
            and self.app_id.get_secret_value().strip()
            and self.secret.get_secret_value().strip()
        )

    def capabilities(self) -> list[Capability]:
        result = []
        for operation in ("fetch_product", "discover_offers", "generate_affiliate_link"):
            verified = operation in self._verified_operations
            reason = (
                "Operação respondeu com sucesso nesta instância; disponibilidade pode mudar."
                if verified
                else "Esquema oficial confirmado; exige habilitação, credenciais e autorização da conta."
            )
            if operation == "generate_affiliate_link" and not self.short_links_enabled:
                reason = "Geração exige também a flag específica de links curtos."
            result.append(
                Capability(
                    operation,
                    CapabilityStatus.SUPPORTED
                    if verified
                    else CapabilityStatus.REQUIRES_CREDENTIALS,
                    reason,
                )
            )
        result.append(
            Capability(
                "fetch_coupons",
                CapabilityStatus.UNVERIFIED,
                "O esquema consultado não comprova descoberta ou aplicação de cupons.",
            )
        )
        return result

    def _require_access(self) -> None:
        if not self.enabled or not self.credentials_configured:
            raise ConnectorDisabled("Shopee desativada ou sem AppId/Secret locais configurados.")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.app_id.get_secret_value()):
            raise ConnectorDisabled("AppId Shopee inválido na configuração local.")

    def _request(self, query: str, *, cache: bool = True, retry: bool = True) -> JsonResponse:
        self._require_access()
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise InvalidPayload("O relógio do conector deve informar fuso horário.")
        timestamp = str(math.ceil(now.timestamp()))
        body = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        app_id = self.app_id.get_secret_value()
        secret = self.secret.get_secret_value()
        signature = hashlib.sha256(
            app_id.encode("utf-8") + timestamp.encode("ascii") + body + secret.encode("utf-8")
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"SHA256 Credential={app_id}, Timestamp={timestamp}, Signature={signature}",
        }
        cache_key = (
            f"shopee:{self._cache_scope}:{hashlib.sha256(body).hexdigest()}" if cache else None
        )
        response = self.transport.request(
            "POST", ENDPOINT, headers=headers, body=body, cache_key=cache_key, retry=retry
        )
        if not isinstance(response.data, dict):
            raise InvalidPayload()
        if response.data.get("errors"):
            # Never propagate an upstream message: it can echo submitted credentials or URLs.
            raise IntegrationError("UPSTREAM_GRAPHQL_ERROR", "A API Shopee recusou a operação.")
        if not isinstance(response.data.get("data"), dict):
            raise InvalidPayload()
        if (
            not isinstance(response.fetched_at, datetime)
            or response.fetched_at.tzinfo is None
            or response.fetched_at.utcoffset() is None
        ):
            raise InvalidPayload("A resposta Shopee deve informar data com fuso horário.")
        return response

    def _connection(self, response: JsonResponse, *, page: int, limit: int) -> tuple[list, bool]:
        data = response.data["data"].get("productOfferV2")
        if not isinstance(data, dict):
            raise InvalidPayload()
        nodes, page_info = data.get("nodes"), data.get("pageInfo")
        if not isinstance(nodes, list) or len(nodes) > limit or not isinstance(page_info, dict):
            raise InvalidPayload()
        has_next = page_info.get("hasNextPage")
        reported_page = page_info.get("page", page)
        if type(has_next) is not bool or type(reported_page) is not int or reported_page != page:
            raise InvalidPayload()
        return nodes, has_next

    def _snapshot(
        self, node: dict, observed_at: datetime, *, ref: ProductRef | None = None
    ) -> ProductSnapshot:
        if not isinstance(node, dict):
            raise InvalidPayload()
        shop_id, item_id = _positive_id(node.get("shopId")), _positive_id(node.get("itemId"))
        node_url = node.get("productLink")
        if _url_identity(node_url) != (shop_id, item_id):
            raise InvalidPayload("A URL Shopee retornada diverge do produto consultado.")
        name = node.get("productName")
        if not isinstance(name, str) or not name.strip() or len(name) > 500:
            raise InvalidPayload("Nome de produto Shopee inválido.")
        canonical_url = ref.canonical_url if ref is not None else node_url
        variant_id = ref.variant_id if ref is not None else ""
        current = _money(node.get("price"))
        minimum, maximum = _money(node.get("priceMin")), _money(node.get("priceMax"))
        if minimum is not None and maximum is not None and minimum > maximum:
            raise InvalidPayload("Intervalo de preço Shopee inválido.")
        # No variant is documented in this operation. A range or URL context is unresolved.
        if (
            variant_id
            or urlsplit(canonical_url).query
            or minimum is None
            or maximum is None
            or minimum != maximum
        ):
            current = None
        elif current is not None and current != minimum:
            raise InvalidPayload("Preço Shopee diverge do intervalo informado.")
        raw_categories = node.get("productCatIds")
        if raw_categories is None:
            categories = []
        elif isinstance(raw_categories, list) and len(raw_categories) <= 20:
            categories = [_category_id(category_id) for category_id in raw_categories]
        else:
            raise InvalidPayload("Categorias Shopee incompatíveis com o esquema.")
        evidence = {"category_external_ids": categories}
        for incoming, outgoing in (
            ("commissionRate", "commission_rate"),
            ("commission", "commission_amount"),
        ):
            if (value := _commission(node.get(incoming))) is not None:
                evidence[outgoing] = value
        public_fingerprint = fingerprint(
            {
                "shop_id": shop_id,
                "item_id": item_id,
                "variant_id": variant_id,
                "current_price": current,
                "price_min": minimum,
                "price_max": maximum,
                "categories": categories,
            }
        )
        return ProductSnapshot(
            platform=self.platform,
            external_item_id=f"{shop_id}:{item_id}",
            canonical_url=canonical_url,
            name=name.strip(),
            variant_id=variant_id,
            current_price=current,
            previous_price=None,
            availability=Availability.UNKNOWN,
            price_access=PriceAccess.UNKNOWN,
            currency="BRL",
            source="shopee:official_api",
            source_type=SourceType.OFFICIAL_API,
            source_reference=f"productOfferV2:{shop_id}:{item_id}",
            raw_fingerprint=public_fingerprint,
            evidence_metadata=evidence,
            observed_at=observed_at.astimezone(UTC),
            is_simulated=False,
            category_external_id=categories[-1] if categories else None,
            category_external_name=None,
            image_url=_image(node.get("imageUrl")),
            seller_id=None,
        )

    def fetch_product(self, ref: ProductRef) -> ProductSnapshot:
        self._require_access()
        if ref.platform != self.platform or not isinstance(ref.external_item_id, str):
            raise InvalidPayload("Referência de produto incompatível com Shopee.")
        match = _REFERENCE_ID.fullmatch(ref.external_item_id)
        if match is None:
            raise InvalidPayload("Shopee exige identificador composto loja:item.")
        shop_id, item_id = _positive_id(match.group(1)), _positive_id(match.group(2))
        if _url_identity(ref.canonical_url) != (shop_id, item_id):
            raise InvalidPayload("O identificador informado diverge da URL Shopee.")
        if (
            not isinstance(ref.variant_id, str)
            or len(ref.variant_id) > 100
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in ref.variant_id)
        ):
            raise InvalidPayload("Identificador de variante inválido.")
        query = (
            f"query {{ productOfferV2(shopId:{shop_id},itemId:{item_id},page:1,limit:1) "
            f"{{ nodes {{ {_PRODUCT_FIELDS} }} pageInfo {{ {_PAGE_FIELDS} }} }} }}"
        )
        response = self._request(query)
        nodes, _ = self._connection(response, page=1, limit=1)
        if not nodes:
            raise IntegrationError(
                "PRODUCT_NOT_FOUND", "Produto não encontrado nas ofertas autorizadas da Shopee."
            )
        snapshot = self._snapshot(nodes[0], response.fetched_at, ref=ref)
        if snapshot.external_item_id != ref.external_item_id:
            raise InvalidPayload("A API Shopee retornou outro produto.")
        self._verified_operations.add("fetch_product")
        return snapshot

    def discover_offers(
        self, query: str, *, page: int = 1, limit: int = 20, category: str | None = None
    ) -> DiscoveryPage:
        self._require_access()
        if not isinstance(query, str) or len(query) > 500:
            raise InvalidPayload("Busca Shopee inválida.")
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidPayload("Use página positiva e limite local de 1 a 100.")
        arguments = f"keyword:{json.dumps(query, ensure_ascii=False)},page:{page},limit:{limit}"
        if category is not None:
            arguments += f",productCatId:{_category_id(category)}"
        graphql = (
            f"query {{ productOfferV2({arguments}) "
            f"{{ nodes {{ {_PRODUCT_FIELDS} }} pageInfo {{ {_PAGE_FIELDS} }} }} }}"
        )
        response = self._request(graphql)
        nodes, has_next = self._connection(response, page=page, limit=limit)
        snapshots = [self._snapshot(node, response.fetched_at) for node in nodes]
        if len({row.external_item_id for row in snapshots}) != len(snapshots):
            raise InvalidPayload("A página Shopee contém produtos duplicados.")
        self._verified_operations.add("discover_offers")
        return DiscoveryPage(snapshots, page, has_next)

    def fetch_coupons(self, ref: ProductRef) -> list:
        raise UnsupportedCapability(
            "O esquema oficial consultado não comprova consulta/aplicação de cupons."
        )

    def generate_affiliate_link(self, url: str) -> AffiliateLinkResult:
        self._require_access()
        if not self.short_links_enabled:
            raise ConnectorDisabled(
                "Geração de links Shopee exige habilitação explícita adicional."
            )
        identity = _url_identity(url)  # Existing affiliate links are not conversion input.
        parsed = urlsplit(url)
        if parsed.query:
            raise InvalidPayload("Use uma URL canônica sem parâmetros de afiliado ou rastreamento.")
        query = (
            "mutation { generateShortLink(input:{originUrl:"
            + json.dumps(url, ensure_ascii=False)
            + "}) { shortLink longLink } }"
        )
        response = self._request(query, cache=False, retry=False)
        data = response.data["data"].get("generateShortLink")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("longLink"), str)
            or not data["longLink"]
        ):
            raise InvalidPayload()
        short = data.get("shortLink")
        try:
            validate_affiliate_url(self.platform, short)
            parsed_short = urlsplit(short)
            if parsed_short.hostname not in {"shope.ee", "s.shopee.com.br"}:
                raise ValueError
            if (
                parsed_short.fragment
                or parsed_short.query
                or re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed_short.path) is None
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise InvalidPayload("A API Shopee não retornou um link curto permitido.") from None
        verified = _link_target_verified(data["longLink"], identity)
        if verified:
            self._verified_operations.add("generate_affiliate_link")
        return AffiliateLinkResult(short, "SHOPEE_AFFILIATE_API", account_verified=verified)

"""Pure functions of the beta mode: parse Shopee nodes, filter, score and render.

No I/O here. Every value shown in a message comes straight from the official
Shopee Affiliate API response; nothing (original price, stock, coupon) is inferred.
"""

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import urlsplit

from app.services.message_templates import (
    ADVERTISING_LABEL,
    MessageTemplateError,
    RenderedMessage,
    telegram_text_length,
    validate_public_url,
)

PRICE_NOTICE = "Preço e estoque podem mudar a qualquer momento."
_OFFER_LINK_HOSTS = {"s.shopee.com.br", "shope.ee", "shopee.com.br", "www.shopee.com.br"}
_ID = re.compile(r"[1-9][0-9]{0,18}\Z")
_MAX_TITLE = 120


class OfferRejected(ValueError):
    """A node that cannot be published; `reason` is a short stable code."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class BetaOffer:
    shop_id: str
    item_id: str
    title: str
    price_min: Decimal
    price_max: Decimal | None
    discount_percent: int
    rating: Decimal | None
    sales: int | None
    commission_rate: Decimal | None
    offer_link: str
    image_url: str | None
    shop_name: str | None = None

    @property
    def item_key(self) -> str:
        return f"{self.shop_id}:{self.item_id}"


def _decimal(value, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OfferRejected(f"INVALID_{field}")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise OfferRejected(f"INVALID_{field}") from None
    if not number.is_finite() or number < 0:
        raise OfferRejected(f"INVALID_{field}")
    return number


def _identifier(value) -> str:
    text = str(value) if type(value) is int else value
    if not isinstance(text, str) or not _ID.fullmatch(text):
        raise OfferRejected("INVALID_ID")
    return text


def parse_offer(node, *, secret: str | None = None) -> BetaOffer:
    if not isinstance(node, dict):
        raise OfferRejected("INVALID_NODE")
    shop_id, item_id = _identifier(node.get("shopId")), _identifier(node.get("itemId"))
    title = node.get("productName")
    if not isinstance(title, str) or not title.strip():
        raise OfferRejected("MISSING_TITLE")
    title = " ".join(title.split())
    if any(ord(c) < 32 or ord(c) == 127 for c in title):
        raise OfferRejected("INVALID_TITLE")
    price_min = _decimal(node.get("priceMin"), field="PRICE") or _decimal(
        node.get("price"), field="PRICE"
    )
    if price_min is None or price_min <= 0:
        raise OfferRejected("MISSING_PRICE")
    price_max = _decimal(node.get("priceMax"), field="PRICE")
    if price_max is not None and price_max < price_min:
        raise OfferRejected("INVALID_PRICE_RANGE")
    discount = node.get("priceDiscountRate")
    if discount is None:
        discount = 0
    if type(discount) is not int or not 0 <= discount < 100:
        raise OfferRejected("INVALID_DISCOUNT")
    rating = _decimal(node.get("ratingStar"), field="RATING")
    if rating is not None and rating > 5:
        raise OfferRejected("INVALID_RATING")
    sales = node.get("sales")
    if sales is not None and (type(sales) is not int or sales < 0):
        raise OfferRejected("INVALID_SALES")
    commission = _decimal(node.get("commissionRate"), field="COMMISSION")
    link = node.get("offerLink")
    try:
        validate_public_url(link)
    except MessageTemplateError:
        raise OfferRejected("MISSING_OFFER_LINK") from None
    if urlsplit(link).hostname not in _OFFER_LINK_HOSTS or (secret and secret in link):
        raise OfferRejected("INVALID_OFFER_LINK")
    image = node.get("imageUrl")
    try:
        image = validate_public_url(image) if image else None
    except MessageTemplateError:
        image = None
    shop_name = node.get("shopName")
    shop_name = " ".join(shop_name.split()) if isinstance(shop_name, str) else None
    return BetaOffer(
        shop_id=shop_id,
        item_id=item_id,
        title=title[:500],
        price_min=price_min.quantize(Decimal("0.01")),
        price_max=price_max.quantize(Decimal("0.01")) if price_max is not None else None,
        discount_percent=discount,
        rating=rating.quantize(Decimal("0.01")) if rating is not None else None,
        sales=sales,
        commission_rate=commission,
        offer_link=link,
        image_url=image,
        shop_name=shop_name or None,
    )


def rejection_reason(offer: BetaOffer, settings) -> str | None:
    """First filter the offer fails, or None when it passes every filter."""
    if offer.discount_percent < settings.beta_min_discount_percent:
        return "LOW_DISCOUNT"
    if settings.beta_min_rating > 0 and (
        offer.rating is None or offer.rating < settings.beta_min_rating
    ):
        return "LOW_RATING"
    if settings.beta_min_sales > 0 and (
        offer.sales is None or offer.sales < settings.beta_min_sales
    ):
        return "LOW_SALES"
    if settings.beta_min_commission_rate > 0 and (
        offer.commission_rate is None or offer.commission_rate < settings.beta_min_commission_rate
    ):
        return "LOW_COMMISSION"
    if offer.price_min < settings.beta_min_price:
        return "PRICE_TOO_LOW"
    if offer.price_min > settings.beta_max_price:
        return "PRICE_TOO_HIGH"
    return None


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _stem(token: str) -> str:
    """Fold Portuguese gender/plural endings: feminino/feminina/femininas -> feminin."""
    if len(token) >= 5:
        for suffix in ("as", "os", "es", "a", "o", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                return token[: -len(suffix)]
    return token


def _tokens(text: str) -> set[str]:
    return {_stem(token) for token in re.findall(r"[a-z0-9]+", _normalize(text))}


def has_excluded_term(title: str, excluded_terms=()) -> bool:
    """True when every significant word of any excluded term appears in the title."""
    tokens = _tokens(title)
    for term in excluded_terms:
        wanted = {token for token in _tokens(term) if len(token) >= 2}
        if wanted and wanted <= tokens:
            return True
    return False


def matches_keyword(title: str, keyword: str, broad_keywords=()) -> bool:
    """Title contains every significant word of the keyword (accents/case ignored).

    Broad keywords ("geral", "moda"...) describe a category, not words in a title,
    so they always match. Single characters ("5" in "playstation 5") are ignored.
    """
    if _normalize(" ".join(keyword.split())) in {_normalize(b) for b in broad_keywords}:
        return True
    wanted = {token for token in _tokens(keyword) if len(token) >= 2}
    return wanted <= _tokens(title) if wanted else True


def score(offer: BetaOffer) -> Decimal:
    """Higher is better: discount first, then rating, popularity and commission."""
    value = Decimal(offer.discount_percent)
    if offer.rating is not None:
        value += (offer.rating - Decimal("4")) * 10
    if offer.sales:
        value += Decimal(str(round(math.log10(offer.sales + 1) * 5, 2)))
    if offer.commission_rate is not None:
        value += min(offer.commission_rate, Decimal("1")) * 20
    return value.quantize(Decimal("0.01"))


def _money(value: Decimal) -> str:
    formatted = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {formatted}"


def _sales(value: int) -> str:
    if value >= 1000:
        thousands = f"{value / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{thousands} mil vendidos"
    return f"{value} vendido" + ("s" if value != 1 else "")


def render(offer: BetaOffer, *, ad_label: bool = True) -> RenderedMessage:
    title = offer.title if len(offer.title) <= _MAX_TITLE else offer.title[: _MAX_TITLE - 1] + "…"
    lines = []
    if offer.discount_percent > 0:
        lines.append(f"🔥 <b>{offer.discount_percent}% OFF</b>")
    lines.append(f"<b>{escape(title, quote=True)}</b>")
    prefix = "A partir de " if offer.price_max and offer.price_max > offer.price_min else ""
    lines.append(f"💰 {prefix}<b>{_money(offer.price_min)}</b>")
    details = []
    if offer.rating is not None and offer.rating > 0:
        details.append(f"⭐ {str(offer.rating.normalize()).replace('.', ',')}")
    if offer.sales:
        details.append(_sales(offer.sales))
    if details:
        lines.append(" · ".join(details))
    lines.append(f'🛒 <a href="{escape(offer.offer_link, quote=True)}">Comprar na Shopee</a>')
    lines.append("")
    lines.append(PRICE_NOTICE)
    if ad_label:
        lines.append(ADVERTISING_LABEL)
    text = "\n".join(lines)
    photo = offer.image_url
    if photo and telegram_text_length(text) > 1024:
        photo = None
    if telegram_text_length(text) > 4096:
        raise MessageTemplateError("Mensagem excede o limite de texto do Telegram.")
    return RenderedMessage(text, photo_url=photo)


_WA_FORMATTING = re.compile(r"[*_~`]")


def render_whatsapp(offer: BetaOffer, *, ad_label: bool = True) -> RenderedMessage:
    """Same content as `render`, in WhatsApp formatting (*bold*, plain link)."""
    title = _WA_FORMATTING.sub("", offer.title)
    title = " ".join(title.split()) or "Oferta"
    if len(title) > _MAX_TITLE:
        title = title[: _MAX_TITLE - 1] + "…"
    lines = []
    if offer.discount_percent > 0:
        lines.append(f"🔥 *{offer.discount_percent}% OFF*")
    lines.append(f"*{title}*")
    prefix = "A partir de " if offer.price_max and offer.price_max > offer.price_min else ""
    lines.append(f"💰 {prefix}*{_money(offer.price_min)}*")
    details = []
    if offer.rating is not None and offer.rating > 0:
        details.append(f"⭐ {str(offer.rating.normalize()).replace('.', ',')}")
    if offer.sales:
        details.append(_sales(offer.sales))
    if details:
        lines.append(" · ".join(details))
    lines.append(f"🛒 Comprar na Shopee: {offer.offer_link}")
    lines.append("")
    lines.append(f"_{PRICE_NOTICE}_")
    if ad_label:
        lines.append(ADVERTISING_LABEL)
    return RenderedMessage("\n".join(lines), photo_url=offer.image_url, parse_mode="WHATSAPP")

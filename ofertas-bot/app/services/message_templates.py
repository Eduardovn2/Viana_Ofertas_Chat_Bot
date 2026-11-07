"""Deterministic, escaped previews for the regular Telegram HTML message API.

Official limits and supported entities: https://core.telegram.org/bots/api#formatting-options
No database lookups, URL fetches, price estimates or coupon evaluation happen here.
"""

import ipaddress
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

ADVERTISING_LABEL = "Publicidade • link de afiliado"
IMAGE_OMITTED_NOTICE = "Imagem omitida: mensagem excede o limite de legenda."
_INVALID_ENTITY = re.compile(r"&(?!amp;|lt;|gt;|quot;|#[0-9]+;|#x[0-9a-fA-F]+;)")
_SAFE_TAG = re.compile(r'</?(?:b|s|code)>|<a href="[^"<>]*">|</a>')


class MessageTemplateError(ValueError):
    """Static diagnostics suitable for the administrative API."""


@dataclass(frozen=True)
class MessageData:
    name: str
    current_price: Decimal
    affiliate_url: str
    previous_price: Decimal | None = None
    coupon_text: str | None = None
    installments_text: str | None = None
    image_url: str | None = None
    content_notice: str | None = None


@dataclass(frozen=True)
class RenderedMessage:
    text: str
    photo_url: str | None = None
    parse_mode: str = "HTML"
    image_omitted: bool = False


@dataclass(frozen=True)
class AnnouncementMessageData:
    name: str
    announced_price: Decimal
    affiliate_url: str
    image_url: str | None = None
    confirmed_title: str | None = None
    documented_options: tuple[str, ...] = ()
    documented_metrics: tuple[str, ...] = ()


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MessageTemplateError("Texto obrigatório ausente.")
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value):
        raise MessageTemplateError("Texto contém caracteres de controle inválidos.")
    try:
        value.encode("utf-16-le")
    except UnicodeError:
        raise MessageTemplateError("Texto contém caracteres inválidos.") from None
    return value.strip()


def validate_public_url(value: str) -> str:
    """HTTPS URL only; no credentials, local hosts, IP literals or hidden controls."""
    if not isinstance(value, str) or not value or len(value) > 2048 or value != value.strip():
        raise MessageTemplateError("URL HTTPS inválida.")
    if any(char in value for char in "\\\"'<>") or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise MessageTemplateError("URL HTTPS inválida.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.endswith(":")
            or "." not in host
            or host.endswith((".", ".localhost", ".local", ".internal"))
            or parsed.fragment
        ):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                raise ValueError from None
        else:
            raise ValueError
    except (ValueError, TypeError):
        raise MessageTemplateError("URL HTTPS inválida.") from None
    return value


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"b", "s", "a", "code"}:
            raise MessageTemplateError("HTML de mensagem inválido.")
        if tag == "a":
            if len(attrs) != 1 or attrs[0][0] != "href" or "a" in self.stack:
                raise MessageTemplateError("HTML de mensagem inválido.")
            validate_public_url(attrs[0][1])
        elif attrs:
            raise MessageTemplateError("HTML de mensagem inválido.")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise MessageTemplateError("HTML de mensagem inválido.")

    def handle_startendtag(self, tag, attrs):
        raise MessageTemplateError("HTML de mensagem inválido.")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_comment(self, data):
        raise MessageTemplateError("HTML de mensagem inválido.")

    def handle_decl(self, decl):
        raise MessageTemplateError("HTML de mensagem inválido.")

    def handle_pi(self, data):
        raise MessageTemplateError("HTML de mensagem inválido.")


def telegram_text_length(text: str) -> int:
    """Count decoded visible text conservatively in Telegram's UTF-16 entity units."""
    _text(text)
    if _INVALID_ENTITY.search(text):
        raise MessageTemplateError("Entidade HTML inválida.")
    plain_source = _SAFE_TAG.sub("", text)
    if "<" in plain_source or ">" in plain_source:
        raise MessageTemplateError("HTML de mensagem inválido.")
    parser = _HTMLText()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise MessageTemplateError("HTML de mensagem inválido.")
    visible = "".join(parser.parts)
    if not visible.strip():
        raise MessageTemplateError("Mensagem vazia.")
    return len(visible.encode("utf-16-le")) // 2


def _money(value: Decimal) -> str:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or not 0 < value <= Decimal("9999999999.99")
    ):
        raise MessageTemplateError("Preço deve ser Decimal positivo e finito.")
    try:
        cents = value.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise MessageTemplateError("Preço inválido.") from None
    if cents != value:
        raise MessageTemplateError("Preço deve ter precisão de centavos.")
    formatted = f"{cents:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {formatted}"


def render_message(data: MessageData) -> RenderedMessage:
    if not isinstance(data, MessageData):
        raise MessageTemplateError("Dados da mensagem inválidos.")
    name = escape(_text(data.name), quote=True)
    affiliate = escape(validate_public_url(data.affiliate_url), quote=True)
    lines = [f"<b>{name}</b>", f"Preço atual: <b>{_money(data.current_price)}</b>"]
    if data.previous_price is not None:
        lines.append(f"Preço anterior: <s>{_money(data.previous_price)}</s>")
    if data.coupon_text:
        lines.append(
            f"Cupom confirmado: <code>{escape(_text(data.coupon_text), quote=True)}</code>"
        )
    if data.installments_text:
        lines.append(f"Parcelamento: {escape(_text(data.installments_text), quote=True)}")
    if data.content_notice:
        lines.append(escape(_text(data.content_notice), quote=True))
    lines.extend([f'<a href="{affiliate}">Ver oferta pelo link de afiliado</a>', ADVERTISING_LABEL])
    text = "\n".join(lines)
    photo = validate_public_url(data.image_url) if data.image_url else None
    omitted = photo is not None and telegram_text_length(text) > 1024
    if omitted:
        photo = None
        text += f"\n{IMAGE_OMITTED_NOTICE}"
    if telegram_text_length(text) > 4096:
        raise MessageTemplateError("Mensagem excede o limite de texto do Telegram.")
    return RenderedMessage(text, photo_url=photo, image_omitted=omitted)


def render_announcement_message(data: AnnouncementMessageData) -> RenderedMessage:
    """Informational V2, with attribution and no variant/stock/discount assertions.

    confirmed_title/options/metrics are view data from validated documentary
    evidence, never title inferences or raw seller statistics.
    """
    name = " ".join(_text(data.name).split())
    title = _text(data.confirmed_title) if data.confirmed_title else " ".join(name.split()[:4])
    if not data.confirmed_title and title != name:
        title += "…"
    # Keep the complete source title below when the heading is shortened.
    if len(title) > 76:
        title = title[:73].rsplit(" ", 1)[0].rstrip() + "…"
    affiliate_url = validate_public_url(data.affiliate_url)
    parsed = urlsplit(affiliate_url)
    if parsed.hostname not in {"s.shopee.com.br", "shopee.com.br", "www.shopee.com.br"}:
        raise MessageTemplateError("Link afiliado Shopee inválido.")
    icon = "🔌" if name.casefold().startswith("cabo hdmi") else "🛍"
    lines = [f"{icon} <b>ANÚNCIO • {escape(title)}</b>"]
    if not data.confirmed_title or name != data.confirmed_title:
        lines.extend(["", f"Título no anúncio: {escape(name)}"])
    if data.documented_options:
        lines.extend(
            [
                "",
                "📦 Opções documentadas no anúncio: "
                + escape(" • ".join(_text(v) for v in data.documented_options)),
            ]
        )
    if data.documented_metrics:
        lines.extend(["", *[escape(_text(v)) for v in data.documented_metrics]])
    lines.extend(
        [
            "",
            "💰 <b>PREÇO ANUNCIADO:</b>",
            f"<b>{_money(data.announced_price)}</b>",
            "",
            "🛒 <b>CONFIRA NA SHOPEE:</b>",
            f'<a href="{escape(affiliate_url, quote=True)}">{escape(affiliate_url)}</a>',
            "",
            "⚠️ Confira a versão, o conteúdo, o preço final, o frete e a disponibilidade.",
            "Preço final e estoque não verificados.",
            "",
            ADVERTISING_LABEL,
        ]
    )
    text = "\n".join(lines)
    photo = None
    if data.image_url:
        # Same official image allowlist as the Shopee connector; never fetch here.
        try:
            candidate = validate_public_url(data.image_url)
            host = urlsplit(candidate).hostname or ""
            if host == "cf.shopee.com.br" or host.endswith(".susercontent.com"):
                photo = candidate
        except MessageTemplateError:
            pass
    omitted = photo is not None and telegram_text_length(text) > 1024
    if omitted:
        photo = None
        text += "\n" + IMAGE_OMITTED_NOTICE
    if telegram_text_length(text) > 4096:
        raise MessageTemplateError("Mensagem excede o limite de texto do Telegram.")
    return RenderedMessage(text, photo_url=photo, image_omitted=omitted)

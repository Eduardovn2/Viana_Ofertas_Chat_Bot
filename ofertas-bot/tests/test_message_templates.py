"""Local rendering only: all example prices and links are SYNTHETIC fixtures."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from app.services.message_templates import (
    ADVERTISING_LABEL,
    IMAGE_OMITTED_NOTICE,
    MessageData,
    MessageTemplateError,
    render_message,
    telegram_text_length,
)

BASE = MessageData("Kit sintético", Decimal("99.90"), "https://meli.la/SYNTHETIC")
IMAGE = "https://http2.mlstatic.com/SYNTHETIC.jpg"


def test_required_fields_and_advertising_without_unknown_optional_details():
    message = render_message(BASE)
    assert "<b>Kit sintético</b>" in message.text
    assert "R$ 99,90" in message.text
    assert ADVERTISING_LABEL in message.text
    assert 'href="https://meli.la/SYNTHETIC"' in message.text
    for absent in ("Cupom", "Parcelamento", "anterior", "None", "Imagem omitida"):
        assert absent not in message.text
    assert message.photo_url is None
    assert message.parse_mode == "HTML"


def test_optional_fields_render_only_known_inputs_without_calculating_discount():
    message = render_message(
        replace(
            BASE,
            current_price=Decimal("1234.50"),
            previous_price=Decimal("1500"),
            coupon_text="SYNTHETIC10",
            installments_text="10x de R$ 123,45 sem juros",
            image_url=IMAGE,
        )
    )
    assert "R$ 1.234,50" in message.text
    assert "<s>R$ 1.500,00</s>" in message.text
    assert "<code>SYNTHETIC10</code>" in message.text
    assert "10x de R$ 123,45 sem juros" in message.text
    assert message.photo_url == IMAGE
    assert not message.image_omitted
    assert "%" not in message.text


def test_all_dynamic_text_and_link_attributes_are_escaped():
    message = render_message(
        replace(
            BASE,
            name='<b>Nome & "especial"</b>',
            coupon_text="<script>&",
            installments_text='2x < 5 & "oferta"',
            affiliate_url="https://meli.la/SYNTHETIC?a=1&b=2",
        )
    )
    assert "&lt;b&gt;Nome &amp; &quot;especial&quot;&lt;/b&gt;" in message.text
    assert "&lt;script&gt;&amp;" in message.text
    assert "2x &lt; 5 &amp; &quot;oferta&quot;" in message.text
    assert "?a=1&amp;b=2" in message.text
    assert "<script>" not in message.text


@pytest.mark.parametrize(
    "price",
    [
        None,
        "99.90",
        99.9,
        True,
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1.001"),
        Decimal("10000000000"),
    ],
)
def test_reject_invalid_or_non_decimal_prices(price):
    with pytest.raises(MessageTemplateError):
        render_message(replace(BASE, current_price=price))


@pytest.mark.parametrize("name", [None, "", "  ", "nome\x00", "nome\x7f", "\ud800"])
def test_reject_missing_or_invalid_required_name(name):
    with pytest.raises(MessageTemplateError):
        render_message(replace(BASE, name=name))


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://meli.la/code",
        "javascript:alert(1)",
        "https://user:secret@meli.la/code",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://host.local/x",
        "https://meli.la:8443/code",
        "https://meli.la:/code",
        "https://meli.la/code#fragment",
        'https://meli.la/"onclick',
        "https://meli.la/\\code",
        "https://meli.la/code\n",
    ],
)
def test_reject_unsafe_affiliate_urls_without_resolution(url):
    with pytest.raises(MessageTemplateError):
        render_message(replace(BASE, affiliate_url=url))


def _at_visible_length(length, *, image=None, character="x"):
    overhead = telegram_text_length(render_message(replace(BASE, name="x")).text) - 1
    return render_message(replace(BASE, name=character * (length - overhead), image_url=image))


def test_caption_boundary_counts_entities_after_parsing():
    message = _at_visible_length(1024, image=IMAGE, character="&")
    assert len(message.text) > 4096
    assert telegram_text_length(message.text) == 1024
    assert message.photo_url == IMAGE


def test_long_caption_becomes_explicit_text_preview_before_approval():
    message = _at_visible_length(1025, image=IMAGE)
    assert message.photo_url is None
    assert message.image_omitted is True
    assert IMAGE_OMITTED_NOTICE in message.text
    assert telegram_text_length(message.text) <= 4096


def test_text_boundary_is_exact_and_never_splits_or_truncates():
    assert telegram_text_length(_at_visible_length(4096).text) == 4096
    with pytest.raises(MessageTemplateError, match="limite"):
        _at_visible_length(4097)


def test_image_omission_notice_must_also_fit_text_limit():
    with pytest.raises(MessageTemplateError, match="limite"):
        _at_visible_length(4096, image=IMAGE)


def test_utf16_units_are_conservative_for_emoji_and_entities():
    assert telegram_text_length("<b>🧡&amp;&lt;</b>") == 4


@pytest.mark.parametrize(
    "html",
    [
        "<script>x</script>",
        "<b onclick='x'>x</b>",
        "<b>x",
        "<b",
        "<b>x</s>",
        "x &copy;",
        '<a href="javascript:alert(1)">x</a>',
        "<!--secret-->x",
        "<b/>",
        "<b></b>",
    ],
)
def test_reject_unexpected_html_contract(html):
    with pytest.raises(MessageTemplateError):
        telegram_text_length(html)


def test_data_and_rendered_payload_are_immutable_and_deterministic():
    message = render_message(BASE)
    assert message == render_message(BASE)
    with pytest.raises(FrozenInstanceError):
        message.text = "modified"
    with pytest.raises(FrozenInstanceError):
        BASE.name = "modified"

"""Professional informational messages; no live Telegram or image downloads."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import test_announcements as announcements
import test_auto_pilot_real as real

from app.services.message_templates import (
    AnnouncementMessageData,
    MessageData,
    MessageTemplateError,
    render_announcement_message,
    render_message,
    telegram_text_length,
)

base_context = real.base_context
context = real.context
no_network = real.no_network
BASE = AnnouncementMessageData(
    name="Cabo HDMI de 1 metro",
    announced_price=Decimal("59.00"),
    affiliate_url="https://s.shopee.com.br/synthetic",
)
IMAGE = "https://cf.shopee.com.br/file/synthetic-image"


def test_professional_photo_and_text_templates():
    plain = render_announcement_message(BASE)
    photo = render_announcement_message(replace(BASE, image_url=IMAGE))
    assert plain.photo_url is None and photo.photo_url == IMAGE
    assert "<b>PREÇO ANUNCIADO:</b>\n<b>R$ 59,00</b>" in photo.text
    assert "Publicidade • link de afiliado" in photo.text
    assert "Preço final e estoque não verificados." in photo.text
    assert telegram_text_length(photo.text) <= 1024


def test_long_title_short_heading_preserves_full_identity():
    name = "Marca Modelo 2.1 8K 1M " + "Especificação " * 24 + "VERSÃO ESPECIAL"
    rendered = render_announcement_message(replace(BASE, name=name))
    assert "…</b>" in rendered.text.split("\n")[0]
    assert name.strip() in rendered.text
    assert "VERSÃO ESPECIAL" in rendered.text


def test_documented_fields_only_and_no_automatic_discounts():
    rendered = render_announcement_message(
        replace(
            BASE,
            confirmed_title="Cabo HDMI de 1 metro",
            documented_options=("8K Aquário 1M", "Full HD 1M"),
            documented_metrics=("⭐ Avaliação do produto: 4,8/5 • 10 avaliações documentadas",),
        )
    )
    assert "8K Aquário 1M" in rendered.text and "Full HD 1M" in rendered.text
    assert "4,8/5" in rendered.text
    assert "%" not in rendered.text and "vendidos" not in rendered.text
    plain = render_announcement_message(BASE)
    assert "avaliações" not in plain.text and "Opções documentadas" not in plain.text
    verified = render_message(
        MessageData(
            BASE.name, BASE.announced_price, BASE.affiliate_url, previous_price=Decimal("79.00")
        )
    )
    assert "<s>R$ 79,00</s>" in verified.text
    assert "PREÇO ANUNCIADO" not in verified.text


def test_html_escaped_and_validated():
    rendered = render_announcement_message(
        replace(BASE, name='Cabo <HDMI> & "1M"', affiliate_url=BASE.affiliate_url + "?a=1&b=2")
    )
    assert "&lt;HDMI&gt;" in rendered.text and "&amp;" in rendered.text
    assert "&quot;1M&quot;" in rendered.text
    assert 'href="https://s.shopee.com.br/synthetic?a=1&amp;b=2"' in rendered.text
    assert telegram_text_length(rendered.text) > 0


@pytest.mark.parametrize(
    "url",
    [
        "http://s.shopee.com.br/a",
        "https://evil.example/a",
        'https://s.shopee.com.br/a" onclick="bad',
        "https://127.0.0.1/a",
    ],
)
def test_invalid_affiliate_urls_rejected(url):
    with pytest.raises(MessageTemplateError):
        render_announcement_message(replace(BASE, affiliate_url=url))


def test_invalid_or_unofficial_image_is_omitted():
    rendered = render_announcement_message(replace(BASE, image_url="https://other.example/a.png"))
    assert rendered.photo_url is None


def test_caption_overflow_uses_one_text_message_not_two_attempts():
    rendered = render_announcement_message(
        replace(
            BASE,
            image_url=IMAGE,
            documented_options=tuple("Variante " + str(i) + " • " + "x" * 100 for i in range(8)),
        )
    )
    assert rendered.image_omitted and rendered.photo_url is None
    assert telegram_text_length(rendered.text) > 1024
    assert telegram_text_length(rendered.text) <= 4096
    with pytest.raises(MessageTemplateError):
        render_announcement_message(replace(BASE, name="x" * 5000))


@pytest.mark.parametrize("photo", [False, True])
def test_auto_pilot_transport_matches_exact_preview(context, photo):
    announcements.links(context, (1,))
    context["nodes"][0]["imageUrl"] = IMAGE if photo else None
    real.authorized(context, commercial_mode="ANNOUNCEMENT_ONLY", operating_policy={})
    result = context["pilot"].tick(context["pilot_id"])
    assert result["scope"]["plan"]["announcement_template"] == "PRO_V2"
    assert result["counts"]["published"] == 1, result["queue"]
    sent = context["telegram_calls"][0]
    assert ("photo" in sent) == photo
    assert ("caption" in sent) == photo
    assert "PREÇO ANUNCIADO" in sent.get("caption", sent.get("text"))


def test_legacy_authorization_renders_original_message(context):
    announcements.links(context, (1,))
    real.authorized(
        context,
        commercial_mode="ANNOUNCEMENT_ONLY",
        announcement_template="LEGACY_V1",
        operating_policy={},
    )
    result = context["pilot"].tick(context["pilot_id"])
    assert result["counts"]["published"] == 1
    assert context["telegram_calls"][0]["text"].startswith("📢 <b>Publicidade")


def test_documented_rating_semantics_and_unknown_period_are_preserved():
    from app.services.announcements import documented_metric_lines

    now = datetime.now(UTC)
    metric = dict(
        subject_identity="101:201",
        metric="PRODUCT_RATING",
        value="4.8",
        sample_count=1,
        series_key="documented-rating",
        source="OFFICIAL_EXPORT",
        source_url="https://affiliate.shopee.com.br/",
        reference="document:rating",
        source_sha256="a" * 64,
        actor="synthetic_reviewer",
        semantics_reference="document:scale5",
        confirmed_semantics=True,
        observed_at=now.isoformat(),
    )
    lines = documented_metric_lines({"reviews": metric}, "101:201", now)
    assert lines == ("⭐ Avaliação do produto: 4,8/5 • 1 avaliação",)
    assert not documented_metric_lines(
        {"reviews": dict(metric, metric="PRODUCT_RATING_STAR_UNSCALED")}, "101:201", now
    )
    assert not documented_metric_lines({"reviews": metric}, "999:999", now)
    sales = dict(metric, metric="PRODUCT_SALES_UNSPECIFIED_PERIOD", value="150", sample_count=None)
    assert (
        "150 (período não informado)"
        in documented_metric_lines({"popularity": sales}, "101:201", now)[0]
    )


def test_confirmed_heading_never_uses_title_inferences():
    from test_shopee_multistore import FACTS

    from app.services.announcements import confirmed_announcement_title

    assert confirmed_announcement_title({"name": "Cabo HDMI 8K 1M", "attributes": FACTS}) is None
    title = confirmed_announcement_title(
        {
            "attributes": FACTS,
            "evidence_source": "MANUAL_DOCUMENTED",
            "evidence_reference": "document:synthetic",
        }
    )
    assert "brave bull" in title.casefold() and "cockpit x" in title.casefold() and "azul" in title

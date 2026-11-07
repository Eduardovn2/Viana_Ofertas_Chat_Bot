"""Explicit informational policy; never grants verified-commercial eligibility."""

from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from types import SimpleNamespace

from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.models import (
    AppSetting,
    Availability,
    Offer,
    Platform,
    PriceAccess,
    Product,
    ProductPriceHistory,
    SourceType,
)
from app.services.affiliate_pipeline import OWNED_LINK_PREFIX, validate_owned_link_receipt
from app.services.comparison import aware_utc
from app.services.message_templates import (
    AnnouncementMessageData,
    RenderedMessage,
    render_announcement_message,
)
from app.services.offers import has_own_affiliate_link
from app.services.opportunity_ranking import MetricEvidence
from app.services.ranking_signal_import import load_ranking_signals
from app.services.shopee_identity import expected_identity, url_identity

MODE = "ANNOUNCEMENT_ONLY"
RULES = dict(
    version="ANNOUNCEMENT_ONLY_V1",
    publication_mode=MODE,
    max_quote_age_seconds=600,
    require_official_capture=True,
    require_documented_owned_link=True,
    availability_claims=False,
    final_price_claims=False,
    historical_discount_claims=False,
    links_created=0,
    uncertain_never_retries=True,
)


def quote_from_capture(value, capture_id, digest):
    query = next((q for q in value["queries"] if q["id"] == capture_id), None)
    if not query or query["state"] != "SUCCEEDED":
        raise ValueError("OFFICIAL_CAPTURE_REQUIRED")
    capture = query.get("capture", {})
    comparison = capture.get("comparison", {})
    if capture.get("source") != "shopee:official_api" or capture.get(
        "comparison_sha256"
    ) != fingerprint(comparison):
        raise ValueError("OFFICIAL_CAPTURE_INVALID")
    quote = next((q for q in comparison.get("candidates", []) if fingerprint(q) == digest), None)
    if not quote:
        raise ValueError("QUOTE_NOT_IN_OFFICIAL_CAPTURE")
    return quote


def validate_quote(quote, now):
    observed = aware_utc(datetime.fromisoformat(quote["observed_at"]))
    price = Decimal(str(quote["announced_price"]))
    if (
        not now - timedelta(seconds=600) < observed <= now
        or quote["currency"] != "BRL"
        or not price.is_finite()
        or not Decimal("0") < price <= Decimal("9999999999.99")
        or price != price.quantize(Decimal("0.01"))
        or quote["price_status"] != "VALID_ANNOUNCED_PRICE"
        or quote["status"] == "REJECTED"
        or not isinstance(quote["name"], str)
        or not quote["name"].strip()
        or len(quote["name"]) > 500
        or any(ord(c) < 32 for c in quote["name"])
    ):
        raise ValueError("ANNOUNCEMENT_QUOTE_INVALID_OR_STALE")
    return observed, price


def validate_link(session, settings, product, now):
    if (
        product.platform != Platform.SHOPEE
        or product.variant_id
        or url_identity(product.original_url) != expected_identity(product)
        or not has_own_affiliate_link(product, require_verified=True)
        or session.scalar(
            select(AppSetting).where(AppSetting.key == OWNED_LINK_PREFIX + str(product.id))
        )
        is None
    ):
        raise ValueError("DOCUMENTED_LISTING_LINK_REQUIRED")
    validate_owned_link_receipt(session, settings, product, now)


def create_announcement(session, settings, value, pilot_id, capture_id, quote, now):
    """Append a true API observation; never create ManualOfferDetails/verified flags."""
    if value["scope"]["plan"].get("commercial_mode") != MODE:
        raise ValueError("ANNOUNCEMENT_SCOPE_REQUIRED")
    digest = fingerprint(quote)
    quote_from_capture(value, capture_id, digest)
    observed, price = validate_quote(quote, now)
    products = session.scalars(
        select(Product).where(
            Product.platform == Platform.SHOPEE,
            Product.external_id == quote["identity"],
            Product.variant_id == "",
        )
    ).all()
    if len(products) != 1:
        raise ValueError("DOCUMENTED_LISTING_LINK_REQUIRED")
    product = products[0]
    validate_link(session, settings, product, now)
    evidence = dict(
        mode=MODE,
        pilot_id=str(pilot_id),
        capture_id=capture_id,
        quote_sha256=digest,
        quote=quote,
        link_sha256=fingerprint({"url": product.affiliate_link.url}),
        rules_sha256=fingerprint(RULES),
    )
    if value["scope"]["plan"].get("announcement_template") == "PRO_V2":
        evidence["template"] = "PRO_V2"
        evidence["presentation_metrics"] = load_ranking_signals(
            session, [quote["identity"]], now=now
        ).get(quote["identity"], {})
    history = ProductPriceHistory(
        product_id=product.id,
        current_price=price,
        currency="BRL",
        availability=Availability.UNKNOWN,
        price_access=PriceAccess.UNKNOWN,
        source="shopee:announcement_only",
        source_type=SourceType.OFFICIAL_API,
        observed_at=observed,
        raw_fingerprint=fingerprint(evidence),
        is_simulated=value["scope"].get("mode") == "MOCK_ONLY"
        or value["scope"].get("environment") == "ISOLATED_MOCK",
        evidence_metadata={"announcement_only": evidence},
    )
    session.add(history)
    session.flush()
    offer = Offer(
        product_id=product.id,
        price_history_id=history.id,
        current_price=price,
        expires_at=observed + timedelta(seconds=600),
    )
    session.add(offer)
    session.flush()
    return SimpleNamespace(
        offer=offer,
        product=product,
        history=history,
        details=None,
        coupon=None,
        coupon_evaluation=None,
        presentation_metrics=documented_metric_lines(
            evidence.get("presentation_metrics", {}), product.external_id, now
        ),
    )


def read_announcement(session, settings, offer_id, now):
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise ValueError("ANNOUNCEMENT_NOT_FOUND")
    history = session.get(ProductPriceHistory, offer.price_history_id)
    evidence = (history.evidence_metadata or {}).get("announcement_only")
    if not evidence or evidence.get("rules_sha256") != fingerprint(RULES):
        raise ValueError("ANNOUNCEMENT_POLICY_REQUIRED")
    source = session.scalar(
        select(AppSetting.value).where(AppSetting.key == "auto_pilot:" + evidence["pilot_id"])
    )
    if (
        not source
        or fingerprint(source["scope"]) != source["scope_sha256"]
        or source["scope"]["plan"].get("commercial_mode") != MODE
    ):
        raise ValueError("ANNOUNCEMENT_SOURCE_INVALID")
    quote = quote_from_capture(source, evidence["capture_id"], evidence["quote_sha256"])
    if (evidence.get("template") == "PRO_V2") != (
        source["scope"]["plan"].get("announcement_template") == "PRO_V2"
    ):
        raise ValueError("ANNOUNCEMENT_TEMPLATE_OUTSIDE_SCOPE")
    observed, price = validate_quote(quote, now)
    product = session.get(Product, offer.product_id)
    if (
        evidence["quote"] != quote
        or product.external_id != quote["identity"]
        or history.product_id != product.id
        or history.current_price != price
        or aware_utc(history.observed_at) != observed
        or offer.current_price != price
        or history.raw_fingerprint != fingerprint(evidence)
        or history.availability != Availability.UNKNOWN
        or history.price_access != PriceAccess.UNKNOWN
        or offer.expires_at is None
        or aware_utc(offer.expires_at) <= now
        or any(
            v is not None
            for v in (
                offer.coupon_id,
                offer.previous_price,
                offer.historical_price,
                offer.final_price,
                offer.discount_percentage,
                offer.discount_amount,
            )
        )
    ):
        raise ValueError("ANNOUNCEMENT_BINDING_CHANGED")
    validate_link(session, settings, product, now)
    if evidence["link_sha256"] != fingerprint({"url": product.affiliate_link.url}):
        raise ValueError("ANNOUNCEMENT_LINK_CHANGED")
    return SimpleNamespace(
        offer=offer,
        product=product,
        history=history,
        details=None,
        coupon=None,
        coupon_evaluation=None,
        presentation_metrics=documented_metric_lines(
            evidence.get("presentation_metrics", {}), product.external_id, now
        ),
    )


def documented_metric_lines(signals, identity, now):
    """Present only current, typed, documented product metrics; never seller reputation."""
    lines = []
    for dimension in ("reviews", "popularity"):
        raw = signals.get(dimension)
        if raw is None:
            continue
        try:
            metric = MetricEvidence.model_validate(raw)
        except (ValueError, TypeError):
            continue
        if (
            metric.subject_identity != identity
            or not 0 <= (now - metric.observed_at).total_seconds() < 600
        ):
            continue
        if (
            metric.metric == "PRODUCT_RATING"
            and 1 <= metric.value <= 5
            and metric.sample_count != 0
        ):
            value = str(metric.value.normalize()).replace(".", ",")
            sample = (
                (
                    f" • {metric.sample_count} "
                    + ("avaliação" if metric.sample_count == 1 else "avaliações")
                )
                if metric.sample_count is not None
                else ""
            )
            lines.append(f"⭐ Avaliação do produto: {value}/5{sample}")
        elif metric.metric in {
            "LIFETIME_SALES",
            "WINDOW_SALES",
            "PRODUCT_SALES_UNSPECIFIED_PERIOD",
        }:
            period = (
                "total documentado"
                if metric.metric == "LIFETIME_SALES"
                else f"período de {metric.period_seconds} segundos"
                if metric.metric == "WINDOW_SALES"
                else "período não informado"
            )
            lines.append(f"📊 Vendas informadas: {int(metric.value)} ({period})")
    return tuple(lines)


def evidence_hash(bundle):
    data = bundle.history.evidence_metadata
    return fingerprint(data.get("announcement_only") or data["documented_identity"])


def confirmed_announcement_title(quote):
    """Only comparator-attested facts; textual hints never become confirmed specs."""
    if quote.get("evidence_source") != "MANUAL_DOCUMENTED" or not quote.get("evidence_reference"):
        return None
    from app.services.shopee_multistore import ProductFacts

    try:
        facts = ProductFacts.model_validate(quote.get("attributes"))
    except (ValueError, TypeError):
        return None
    return f"{facts.brand} {facts.model} • {facts.quantity} un. • {facts.variant}"


def render_announcement(bundle, include_image=True):
    quote = bundle.history.evidence_metadata["announcement_only"]["quote"]
    if bundle.history.evidence_metadata["announcement_only"].get("template") == "PRO_V2":
        return render_announcement_message(
            AnnouncementMessageData(
                name=quote["name"],
                announced_price=bundle.offer.current_price,
                affiliate_url=bundle.product.affiliate_link.url,
                image_url=quote.get("image_url") if include_image else None,
                documented_metrics=getattr(bundle, "presentation_metrics", ()),
                confirmed_title=confirmed_announcement_title(quote),
            )
        )
    price = (
        f"{bundle.offer.current_price:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    )
    return RenderedMessage(
        text=(
            "📢 <b>Publicidade • link de afiliado</b>\n\n"
            f"Título do anúncio na Shopee:\n{escape(quote['name'])}\n\n"
            f"<b>Preço anunciado: R$ {price}</b>\n\n"
            "Confira a opção, o conteúdo, o preço aplicável, o frete e a disponibilidade no anúncio. "
            "Preço final e estoque não verificados.\n\n"
            f"{escape(bundle.product.affiliate_link.url)}"
        ),
        image_omitted=True,
    )

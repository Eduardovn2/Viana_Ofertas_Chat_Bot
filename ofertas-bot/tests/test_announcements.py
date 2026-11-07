"""Separate informational policy, real controller with simulated transports."""

import hashlib
import hmac
from datetime import timedelta

import pytest
import test_account_evidence_reuse as account
import test_auto_pilot as offline
import test_auto_pilot_real as real
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.models import AffiliateLink, AppSetting, ManualOfferDetails, Offer, Platform, Product
from app.services.affiliate_pipeline import OWNED_LINK_PREFIX
from app.services.manual_offers import read_manual_offer
from app.services.shopee_evidence import credential_binding, record_account_evidence

base_context = real.base_context
context = real.context
no_network = real.no_network


def links(ctx, numbers=(1, 2)):
    offline.listings(ctx, numbers, verified=False)
    with ctx["factory"]() as session:
        proof = record_account_evidence(
            session, ctx["settings"], account.account_document(ctx), now=ctx["clock"][0]
        )
        for n in numbers:
            product = Product(
                platform=Platform.SHOPEE,
                external_id=f"{100 + n}:{200 + n}",
                variant_id="",
                name=offline.QUERY + f" modelo FICTICIO-{n}",
                original_url=f"https://shopee.com.br/product/{100 + n}/{200 + n}",
                monitoring_enabled=False,
            )
            session.add(product)
            session.flush()
            link = f"https://s.shopee.com.br/synthetic{n}"
            session.add(
                AffiliateLink(product_id=product.id, url=link, source="SHOPEE_OFFICIAL_API")
            )
            receipt = dict(
                credential_fingerprint=credential_binding(ctx["settings"]),
                identity=product.external_id,
                variant_id="",
                original_url_sha256=fingerprint({"original_url": product.original_url}),
                short_link=link,
                account_evidence_id=proof["id"],
            )
            receipt["signature"] = hmac.new(
                ctx["settings"].shopee_secret.get_secret_value().encode(),
                ("commercial-link:" + fingerprint(receipt)).encode(),
                hashlib.sha256,
            ).hexdigest()
            session.add(AppSetting(key=OWNED_LINK_PREFIX + str(product.id), value=receipt))
        session.commit()


def test_multiple_announcements_use_existing_outbox_without_stock_claims(context):
    links(context)
    real.authorized(context, commercial_mode="ANNOUNCEMENT_ONLY", operating_policy={})
    report = context["pilot"].tick(context["pilot_id"])
    assert report["counts"]["published"] == 2, report["findings"]
    for call in context["telegram_calls"]:
        text = call.get("caption", call.get("text"))
        assert "preço anunciado" in text.casefold()
        assert "Preço final e estoque não verificados" in text
        assert "Publicidade" in text
    with context["factory"]() as session:
        for offer in session.scalars(select(Offer)):
            assert session.get(ManualOfferDetails, offer.id) is None
            with pytest.raises((ValueError, IntegrationError)):
                read_manual_offer(session, offer.id, context["settings"], context["clock"][0])


def test_one_missing_receipt_does_not_stop_other_candidate(context):
    links(context)
    with context["factory"]() as session:
        receipt = session.scalar(
            select(AppSetting).where(AppSetting.key.startswith(OWNED_LINK_PREFIX))
        )
        session.delete(receipt)
        session.commit()
    real.authorized(context, commercial_mode="ANNOUNCEMENT_ONLY", operating_policy={})
    report = context["pilot"].tick(context["pilot_id"])
    assert report["counts"]["published"] == 1
    assert any(f["reason"] == "DOCUMENTED_LISTING_LINK_REQUIRED" for f in report["findings"])


def test_verified_mode_is_not_relaxed(context):
    links(context)
    real.authorized(context, commercial_mode="VERIFIED_OFFER", operating_policy={})
    report = context["pilot"].tick(context["pilot_id"])
    assert report["counts"]["published"] == 0


def test_expired_quote_cannot_be_dispatched(context):
    links(context)
    real.authorized(context, commercial_mode="ANNOUNCEMENT_ONLY", operating_policy={})
    context["pilot"].discover_once(context["pilot_id"])
    context["clock"][0] += timedelta(seconds=601)
    report = context["pilot"].dispatch_batch(context["pilot_id"])
    assert report["counts"]["published"] == 0
    assert not context["telegram_calls"]


def test_uncertain_announcement_never_retries(context):
    links(context, (1,))
    real.authorized(context, commercial_mode="ANNOUNCEMENT_ONLY", operating_policy={})
    context["telegram_failure"] = True
    report = context["pilot"].tick(context["pilot_id"])
    assert report["counts"]["uncertain"] == 1
    context["clock"][0] += timedelta(seconds=31)
    context["pilot"].tick(context["pilot_id"])
    assert len(context["telegram_calls"]) == 1


def test_allowlist_and_duplicate_controls(context):
    links(context)
    real.authorized(
        context,
        commercial_mode="ANNOUNCEMENT_ONLY",
        operating_policy={},
        announcement_identities=["101:201"],
    )
    report = context["pilot"].tick(context["pilot_id"])
    assert report["counts"]["published"] == 1
    assert any(f["reason"] == "LISTING_OUTSIDE_ANNOUNCEMENT_SCOPE" for f in report["findings"])
    context["pilot"].dispatch_batch(context["pilot_id"])
    assert len(context["telegram_calls"]) == 1


def test_link_change_after_queue_blocks_dispatch(context):
    links(context, (1,))
    real.authorized(context, commercial_mode="ANNOUNCEMENT_ONLY", operating_policy={})
    context["pilot"].discover_once(context["pilot_id"])
    with context["factory"]() as session:
        product = session.scalar(select(Product))
        product.affiliate_link.url = "https://s.shopee.com.br/changedSynthetic"
        session.commit()
    report = context["pilot"].dispatch_batch(context["pilot_id"])
    assert report["counts"]["published"] == 0
    assert not context["telegram_calls"]


def test_feed_prepare_is_non_consuming_and_account_bound(context):
    from app.prepare_feed_probe import prepare

    links(context, (1,))
    with context["factory"]() as session:
        grant = prepare(session, context["settings"], now=context["clock"][0])
        session.commit()
    assert grant["state"] == "PREPARED" and not grant["consumed"]
    assert grant["requests_reserved"] == 0
    assert grant["scope_sha256"] == fingerprint(grant["scope"])
    assert grant["scope"]["max_http_attempts"] == 1
    assert grant["scope"]["feed_row_reads"] == 0
    assert not context["telegram_calls"]


def test_ranking_selection_precedes_fifo_for_new_authorized_scope(context):
    import copy

    from app.services.auto_pilot import PREFIX

    links(context)
    real.authorized(
        context,
        commercial_mode="ANNOUNCEMENT_ONLY",
        operating_policy={},
        announcement_selection="RANKING_100_V1",
        max_messages=1,
    )
    context["pilot"].discover_once(context["pilot_id"])
    with context["factory"]() as session:
        row = session.scalar(
            select(AppSetting).where(AppSetting.key == PREFIX + context["pilot_id"])
        )
        value = copy.deepcopy(row.value)
        winner = min(value["opportunities"]["analysis_queue"], key=lambda r: r["intelligent_rank"])[
            "identity"
        ]
        value["queue"].sort(key=lambda item: item["identity"] == winner)
        row.value = value
        session.commit()
    result = context["pilot"].dispatch_batch(context["pilot_id"])
    assert result["counts"]["published"] == 1
    assert next(item for item in result["queue"] if item["state"] == "SENT")["identity"] == winner

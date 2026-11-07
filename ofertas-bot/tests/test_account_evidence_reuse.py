"""Reusable account evidence; all documents and links below are synthetic."""

import copy
from datetime import timedelta
from decimal import Decimal

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
from pydantic import SecretStr
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.models import AppSetting, Availability, Offer, PriceAccess, Product, ProductPriceHistory
from app.services.manual_offers import create_manual_offer
from app.services.product_documentation import CORE_CONTENT_NOTICE, DocumentedIdentity
from app.services.shopee_evidence import (
    account_evidence,
    credential_binding,
    link_fingerprint,
    preserved_link_account_readiness,
    record_account_evidence,
)
from app.services.telegram_publication import TelegramPublicationService

base_context = real.base_context
context = real.context
no_network = real.no_network


def account_document(ctx, **changes):
    return (
        dict(
            actor="synthetic_owner",
            account_reference="synthetic-account",
            documented_app_id=ctx["settings"].shopee_app_id,
            reference="document:synthetic-account-proof",
            source_sha256="a" * 64,
            verified_at=ctx["clock"][0],
            expires_at=ctx["clock"][0] + timedelta(days=30),
            confirmed_account_ownership=True,
            confirmed_credential_binding=True,
        )
        | changes
    )


def test_account_proof_idempotent_expiry_rotation_and_no_secrets(context):
    ctx = context
    with ctx["factory"]() as session:
        first = record_account_evidence(
            session, ctx["settings"], account_document(ctx), now=ctx["clock"][0]
        )
        second = record_account_evidence(
            session, ctx["settings"], account_document(ctx), now=ctx["clock"][0]
        )
        session.commit()
        assert first == second
        assert (
            len(list(session.scalars(select(AppSetting).where(AppSetting.key == first["id"])))) == 1
        )
        assert "documented_app_id" not in first and "integrity_signature" not in first
        assert account_evidence(session, ctx["settings"], now=ctx["clock"][0])["id"] == first["id"]
        assert (
            account_evidence(session, ctx["settings"], now=ctx["clock"][0] + timedelta(days=31))
            is None
        )
        rotated = ctx["settings"].model_copy(
            update={"shopee_secret": SecretStr("rotated-synthetic-secret")}
        )
        assert account_evidence(session, rotated, now=ctx["clock"][0]) is None
        row = session.scalar(select(AppSetting).where(AppSetting.key == first["id"]))
        row.value = dict(row.value, account_reference="tampered")
        session.flush()
        assert account_evidence(session, ctx["settings"], now=ctx["clock"][0]) is None


@pytest.mark.parametrize(
    "change",
    [
        {"confirmed_account_ownership": False},
        {"confirmed_credential_binding": 1},
        {"documented_app_id": "wrong-app"},
        {"verified_at": "2026-01-01T00:00:00"},
    ],
)
def test_account_requires_explicit_documented_binding(context, change):
    with context["factory"]() as session, pytest.raises(ValueError):
        record_account_evidence(
            session,
            context["settings"],
            account_document(context, **change),
            now=context["clock"][0],
        )


def test_same_account_reused_but_legacy_link_not_inferred(context):
    ctx = context
    offline.listings(ctx, [1, 2])
    with ctx["factory"]() as session:
        assert account_evidence(session, ctx["settings"], now=ctx["clock"][0]) is None
        record_account_evidence(
            session, ctx["settings"], account_document(ctx), now=ctx["clock"][0]
        )
        for number in (1, 2):
            product = session.scalar(
                select(Product).where(Product.external_id == f"{100 + number}:{200 + number}")
            )
            key = f"shopee_shortlink_once:synthetic-{number}"
            value = dict(
                status="SUCCEEDED",
                result=dict(
                    http_status=200,
                    real_posts=1,
                    shortLink=f"https://s.shopee.com.br/synthetic{number}",
                    longLink=product.original_url,
                ),
            )
            row = AppSetting(key=key, value=value)
            session.add(row)
            session.flush()

            def read(product=product, key=key):
                return preserved_link_account_readiness(
                    session, ctx["settings"], product, key, now=ctx["clock"][0]
                )

            assert read()["blocker"] == "LEGACY_GENERATION_CREDENTIAL_BINDING_MISSING"
            row.value = dict(value, credential_fingerprint=credential_binding(ctx["settings"]))
            session.flush()
            result = read()
            assert result["ownership_reusable"] and not result["operationally_verified"]
            assert not result["publication_authorized"] and result["variant_binding_required"]
            row.value = value
            session.flush()
            record_account_evidence(
                session,
                ctx["settings"],
                account_document(
                    ctx, covered_legacy_link_sha256=[link_fingerprint(value["result"]["shortLink"])]
                ),
                now=ctx["clock"][0],
            )
            assert read()["legacy_link_explicitly_documented"]


def limited_offer(ctx, **changes):
    offline.listings(ctx, [1])
    with ctx["factory"]() as session:
        product = session.scalar(select(Product).where(Product.external_id == "101:201"))
        old = session.scalar(select(Offer).where(Offer.product_id == product.id))
        doc = copy.deepcopy(
            session.get(ProductPriceHistory, old.price_history_id).evidence_metadata[
                "documented_identity"
            ]
        )
        doc.update(
            message_scope="CORE_ATTRIBUTES_ONLY",
            publication_name_sha256=fingerprint({"name": product.name}),
        )
        doc["core_attributes"] = doc.pop("facts")
        doc["core_attributes"]["kit"] = None
        values = dict(
            product_id=product.id,
            current_price=Decimal("100"),
            actor="synthetic_owner",
            verified=True,
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
            now=ctx["clock"][0],
            documented_identity=doc,
        )
        values.update(changes)
        bundle = create_manual_offer(session, ctx["settings"], **values)
        session.commit()
        return bundle.offer.id, DocumentedIdentity.model_validate(doc)


def test_limited_message_is_text_only_explicit_and_not_equivalence(context):
    offer_id, proof = limited_offer(context)
    with context["factory"]() as session:
        service = TelegramPublicationService(context["settings"], clock=lambda: context["clock"][0])
        preview = service.review_message(session, offer_id, context["destination_id"])
        assert CORE_CONTENT_NOTICE in preview["content"]["message"]["text"]
        assert preview["content"]["message"]["photo_url"] is None
        assert not preview["publication_authorized"]
        product = session.scalar(select(Product).where(Product.external_id == "101:201"))
        product.name += " com volante"
        with pytest.raises(ValueError):
            proof.validate_product(product, context["clock"][0])
    other = proof.model_copy(update={"identity": "999:999"})
    assert proof.duplicate_product_key() != other.duplicate_product_key()


@pytest.mark.parametrize(
    "change",
    [
        {"image_url": "https://example.com/accessories.jpg"},
        {"previous_price": Decimal("150")},
        {"verified": False},
        {"availability": Availability.UNKNOWN},
        {"verified_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_limited_message_does_not_bypass_commercial_guards(context, change):
    with pytest.raises((IntegrationError, ValueError, TypeError)):
        limited_offer(context, **change)

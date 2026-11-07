"""Claim-scoped identity on the existing manual offer and guarded pilot paths."""

import copy
from datetime import timedelta
from decimal import Decimal

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.models import Availability, Offer, PriceAccess, Product, ProductPriceHistory
from app.services.manual_offers import create_manual_offer, read_manual_offer
from app.services.product_documentation import DocumentedIdentity

base_context = real.base_context
context = real.context
no_network = real.no_network


def listing_offer(ctx, number=1):
    offline.listings(ctx, [number])
    with ctx["factory"]() as session:
        product = session.scalar(
            select(Product).where(Product.external_id == f"{100 + number}:{200 + number}")
        )
        product.variant_id = ""
        document = dict(
            identity=product.external_id,
            identity_level="LISTING_ALL_OPTIONS",
            variant_id="",
            original_url_sha256=fingerprint({"original_url": product.original_url}),
            publication_name_sha256=fingerprint({"name": product.name}),
            source="MANUAL_DOCUMENTED",
            actor="synthetic_verifier",
            reference="FICTICIO official observation of all selectable options",
            source_sha256=fingerprint({"synthetic_all_options_document": number}),
            verified_at=ctx["clock"][0],
            confirmed_listing_binding=True,
            confirmed_all_options_same_public_terms=True,
            confirmed_all_options_available=True,
            all_options_public_price="100.00",
        )
        bundle = create_manual_offer(
            session,
            ctx["settings"],
            product_id=product.id,
            current_price=Decimal("100.00"),
            actor="synthetic_verifier",
            verified=True,
            availability=Availability.AVAILABLE,
            price_access=PriceAccess.PUBLIC,
            now=ctx["clock"][0],
            documented_identity=document,
        )
        session.commit()
        return bundle.offer.id, DocumentedIdentity.model_validate(document)


def test_listing_without_sku_can_send_only_with_full_all_options_evidence(context):
    ctx = context
    real.authorized(ctx, max_messages=1)
    offer_id, proof = listing_offer(ctx)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["counts"]["published"] == 1
    assert result["queue"][0]["offer_id"] == offer_id
    assert result["queries"][0]["proven_groups"] == 0
    assert proof.facts is None
    assert "synthetic-sku" not in ctx["telegram_calls"][0]["text"]
    assert result["counts"]["queries_reserved"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed_all_options_same_public_terms", None),
        ("confirmed_all_options_available", None),
        ("confirmed_all_options_available", 1),
        ("all_options_public_price", None),
        ("publication_name_sha256", None),
        ("variant_id", "made-up-sku"),
    ],
)
def test_no_sku_is_not_an_unknown_variant_bypass(context, field, value):
    _, proof = listing_offer(context)
    data = proof.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError):
        DocumentedIdentity.model_validate(data)


@pytest.mark.parametrize("change", ["price", "time", "coupon", "historical_discount"])
def test_listing_evidence_cannot_be_reused_for_other_terms(context, change):
    _, proof = listing_offer(context)
    values = dict(price=Decimal("100"), verified_at=context["clock"][0])
    values.update(
        {
            "price": dict(price=Decimal("99")),
            "time": dict(verified_at=context["clock"][0] + timedelta(seconds=1)),
            "coupon": dict(coupon_id=1),
            "historical_discount": dict(previous_price=Decimal("150")),
        }[change]
    )
    with pytest.raises(ValueError):
        proof.validate_commercial(**values)


def test_listing_name_change_invalidates_document_and_preview(context):
    offer_id, _ = listing_offer(context)
    with context["factory"]() as session:
        offer = session.get(Offer, offer_id)
        session.get(Product, offer.product_id).name += " AZUL"
        session.commit()
    with context["factory"]() as session, pytest.raises(IntegrationError):
        read_manual_offer(session, offer_id, context["settings"], context["clock"][0])


def test_listing_keys_do_not_assert_equivalence_between_sellers(context):
    _, proof = listing_offer(context)
    other = proof.model_copy(update={"identity": "999:999"})
    assert proof.duplicate_product_key() != other.duplicate_product_key()


def test_documented_api_title_alias_is_accepted_but_undocumented_is_blocked(context):
    ctx = context
    real.authorized(ctx, max_messages=1)
    offline.listings(ctx, [1])
    api_name = ctx["nodes"][0]["productName"]
    with ctx["factory"]() as session:
        product = session.scalar(select(Product).where(Product.external_id == "101:201"))
        product.name += " Oferta oficial"
        offer = session.scalar(select(Offer).where(Offer.product_id == product.id))
        history = session.get(ProductPriceHistory, offer.price_history_id)
        metadata = copy.deepcopy(history.evidence_metadata)
        metadata["documented_identity"]["documented_observation_names"] = [api_name]
        history.evidence_metadata = metadata
        session.commit()
    assert ctx["pilot"].tick(ctx["pilot_id"])["counts"]["published"] == 1


def test_undocumented_alias_cannot_send(context):
    ctx = context
    real.authorized(ctx, max_messages=1)
    offline.listings(ctx, [1])
    ctx["nodes"][0]["productName"] += " Oferta oficial"
    assert ctx["pilot"].tick(ctx["pilot_id"])["counts"]["published"] == 0


def test_partial_candidate_is_never_imported_as_verified_offer(context):
    ctx = context
    real.authorized(ctx)
    offline.listings(ctx, [1, 2], verified=False)
    report = ctx["pilot"].tick(ctx["pilot_id"])
    assert report["counts"]["published"] == 0
    assert len(report["findings"]) == 2
    with ctx["factory"]() as session:
        assert list(session.scalars(select(Offer))) == []
        assert list(session.scalars(select(ProductPriceHistory))) == []

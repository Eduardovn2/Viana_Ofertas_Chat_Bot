"""One separate read of the documented offerLink; no generateShortLink mutation."""

import pytest
import test_account_evidence_reuse as account
import test_shopee_observation as observation
import test_shopee_single_read as reads

from app.integrations.contracts import ProductRef
from app.integrations.shopee import ShopeeConnector
from app.models import Platform
from app.services.shopee_evidence import record_account_evidence

base_context = reads.base_context
context = reads.context
no_network = reads.no_network


def test_standard_query_bytes_unchanged():
    ref = ProductRef(Platform.SHOPEE, "101:202", "https://shopee.com.br/product/101/202")
    default = ShopeeConnector.product_query(ref)
    assert default == ShopeeConnector.product_query(ref, include_offer_link=False)
    assert "offerLink" not in default
    assert "offerLink" in ShopeeConnector.product_query(ref, include_offer_link=True)


def test_offer_link_read_requires_account_document(context):
    with pytest.raises(ValueError, match="prova vigente"):
        context["single_read"].prepare(
            dict(observation.specification(context), include_offer_link=True), offline=True
        )
    assert context["calls"] == 0


def test_offer_link_read_is_reserved_once_and_persisted_without_commercial_attestation(context):
    ctx = context
    with ctx["factory"]() as session:
        record_account_evidence(
            session, ctx["settings"], account.account_document(ctx), now=ctx["clock"][0]
        )
        session.commit()
    ctx["node"]["offerLink"] = "https://s.shopee.com.br/syntheticReadOnlyLink"
    plan = ctx["single_read"].prepare(
        dict(observation.specification(ctx), include_offer_link=True), offline=True
    )
    assert ctx["calls"] == 0
    reads.approve(ctx, plan)
    result = reads.execute(ctx, plan)
    assert result["status"] == "SUCCEEDED"
    quote = result["quote"]
    assert quote["official_offer_link"] == ctx["node"]["offerLink"]
    assert (
        quote["commercial_verified"] is False
        and quote["link_ownership_automatically_attested"] is False
    )
    persisted = ctx["single_read"].read(plan["authorization_id"])
    assert persisted["result"]["offer_link_observation"]["account_evidence_id"]
    reads.execute(ctx, plan)
    assert ctx["calls"] == 1

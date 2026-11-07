"""Future links + existing offer import/pilot. Only synthetic isolated transports."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import httpx
import pytest
import test_account_evidence_reuse as account
import test_auto_pilot_real as real
import test_official_offer_import as exports
from sqlalchemy import select

from app.models import AffiliateLink, AppSetting, Product, ProductPriceHistory
from app.services.affiliate_pipeline import OWNED_LINK_PREFIX, CommercialLinkFinalizer
from app.services.manual_offers import read_manual_offer
from app.services.shopee_evidence import ACCOUNT_PREFIX, record_account_evidence

base_context = real.base_context
context = real.context
no_network = real.no_network


@pytest.mark.parametrize("timeout", [False, True])
def test_listing_only_link_preflight_never_attests_price_or_creates_offer(context, timeout):
    ctx = context
    service, _ = setup(ctx, timeout=timeout)
    try:
        with ctx["factory"]() as session:
            product = session.scalar(select(Product).where(Product.external_id == "101:201"))
            product.variant_id = ""
            product_id = product.id
            session.commit()
        identifier = str(uuid4())
        baseline = exports.count(ctx, exports.Offer)
        plan = service.prepare_listing_links(
            identifier,
            [product_id],
            actor="synthetic_owner",
            not_before=ctx["clock"][0],
            expires_at=ctx["clock"][0] + timedelta(hours=1),
        )
        assert plan["state"] == "PREPARED" and not ctx["link_calls"]
        approve(service, identifier, plan)
        result = service.execute(identifier)
        assert result["state"] == ("UNCERTAIN" if timeout else "SUCCEEDED")
        assert len(ctx["link_calls"]) == result["requests_reserved"] == 1
        assert exports.count(ctx, exports.Offer) == baseline
        if not timeout:
            assert result["commercial_import"] is None
        with pytest.raises(ValueError):
            service.execute(identifier)
        assert len(ctx["link_calls"]) == 1
    finally:
        service.transport.close()


def setup(ctx, *, timeout=False, proof=True):
    data, row = exports.document(ctx)
    ctx["link_calls"] = []

    def respond(request):
        ctx["link_calls"].append(request)
        if timeout:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/syntheticNewLink",
                        "longLink": "https://shopee.com.br/product/101/201",
                    }
                }
            },
        )

    with ctx["factory"]() as session:
        product = session.scalar(select(Product).where(Product.external_id == "101:201"))
        session.delete(product.affiliate_link)
        if proof:
            record_account_evidence(
                session,
                ctx["settings"],
                account.account_document(ctx, account_reference=data["account_reference"]),
                now=ctx["clock"][0],
            )
        session.commit()
    service = CommercialLinkFinalizer(
        ctx["factory"],
        ctx["settings"],
        clock=lambda: ctx["clock"][0],
        isolated_testing=True,
        transport=httpx.MockTransport(respond),
    )
    return service, exports.encoded(data, [row])


def prepare(ctx, service, document):
    identifier = str(uuid4())
    result = service.prepare(
        identifier,
        document,
        actor="synthetic_owner",
        not_before=ctx["clock"][0],
        expires_at=ctx["clock"][0] + timedelta(seconds=300),
    )
    assert result["state"] == "PREPARED" and not ctx["link_calls"]
    return identifier, result


def approve(service, identifier, plan):
    service.approve(
        identifier,
        scope_sha256=plan["scope_sha256"],
        actor="synthetic_owner",
        confirmed_link_generation=True,
    )


def test_future_link_receipt_import_and_existing_pilot_queue(context):
    ctx = context
    service, document = setup(ctx)
    identifier, plan = prepare(ctx, service, document)
    with pytest.raises(ValueError):
        service.execute(identifier)
    approve(service, identifier, plan)
    result = service.execute(identifier)
    assert result["state"] == "SUCCEEDED"
    assert result["requests_reserved"] == len(ctx["link_calls"]) == 1
    assert result["commercial_import"]["created_offer_ids"]
    with ctx["factory"]() as session:
        receipt = session.scalar(
            select(AppSetting.value).where(AppSetting.key.startswith(OWNED_LINK_PREFIX))
        )
        assert receipt["credential_fingerprint"] and receipt["account_evidence_id"].startswith(
            ACCOUNT_PREFIX
        )
        assert receipt["payload_sha256"] and not receipt["link_selects_variant"]
    # Existing pilot rules/queue/outbox consume the new verified export; no new
    # publication pipeline and no per-message confirmation have been introduced.
    real.authorized(ctx)
    ctx["pilot"].enqueue_documented_imports(ctx["pilot_id"])
    report = ctx["pilot"].report(ctx["pilot_id"])
    assert len(report["queue"]) == 1 and report["queue"][0]["state"] == "QUEUED"
    assert not ctx["telegram_calls"]
    with pytest.raises(ValueError):
        service.execute(identifier)
    assert len(ctx["link_calls"]) == 1


def test_no_account_proof_or_incomplete_commercial_document_no_http(context):
    service, document = setup(context, proof=False)
    with pytest.raises(ValueError):
        prepare(context, service, document)
    assert not context["link_calls"]


def test_timeout_consumes_reservation_and_never_retries(context):
    service, document = setup(context, timeout=True)
    identifier, plan = prepare(context, service, document)
    approve(service, identifier, plan)
    assert service.execute(identifier)["state"] == "UNCERTAIN"
    with pytest.raises(ValueError):
        service.execute(identifier)
    assert len(context["link_calls"]) == 1
    with context["factory"]() as session:
        assert list(session.scalars(select(AffiliateLink))) == []


def test_changed_scope_uuid_repetition_and_expiry_block_http(context):
    ctx = context
    service, document = setup(ctx)
    identifier, plan = prepare(ctx, service, document)
    with pytest.raises(ValueError):
        service.prepare(
            identifier,
            document,
            actor="synthetic_owner",
            not_before=ctx["clock"][0],
            expires_at=ctx["clock"][0] + timedelta(seconds=300),
        )
    approve(service, identifier, plan)
    ctx["clock"][0] += timedelta(seconds=301)
    with pytest.raises(ValueError):
        service.execute(identifier)
    assert not ctx["link_calls"]


def test_account_invalidated_after_generation_blocks_offer_consumption(context):
    ctx = context
    service, document = setup(ctx)
    identifier, plan = prepare(ctx, service, document)
    approve(service, identifier, plan)
    offer_id = service.execute(identifier)["commercial_import"]["created_offer_ids"][0]
    with ctx["factory"]() as session:
        proof = session.scalar(select(AppSetting).where(AppSetting.key.startswith(ACCOUNT_PREFIX)))
        proof.value = dict(proof.value, account_reference="tampered")
        session.commit()
        with pytest.raises(Exception) as failure:
            read_manual_offer(session, offer_id, ctx["settings"], ctx["clock"][0])
        assert getattr(failure.value, "code", None) == "MANUAL_OFFER_INVALID"


def test_concurrent_execute_cannot_cancel_owner_or_send_twice(context):
    ctx = context
    service, document = setup(ctx)
    identifier, plan = prepare(ctx, service, document)
    approve(service, identifier, plan)
    arrived, resume = Event(), Event()
    original = service.transport.handler

    def hold(request):
        arrived.set()
        assert resume.wait(10)
        return original(request)

    service.transport = httpx.MockTransport(hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(service.execute, identifier)
        assert arrived.wait(10)
        try:
            with pytest.raises(ValueError):
                service.execute(identifier)
            assert service.report(identifier)["state"] == "CLAIMED"
        finally:
            resume.set()
        assert running.result(timeout=20)["state"] == "SUCCEEDED"
    assert len(ctx["link_calls"]) == 1


def test_scope_tampering_blocks_before_reservation(context):
    from app.integrations.contracts import fingerprint
    from app.services.affiliate_pipeline import REAL_LINK_PREFIX

    service, document = setup(context)
    identifier, plan = prepare(context, service, document)
    approve(service, identifier, plan)
    with context["factory"]() as session:
        row = session.scalar(
            select(AppSetting).where(AppSetting.key == REAL_LINK_PREFIX + identifier)
        )
        value = copy.deepcopy(row.value)
        value["scope"]["max_posts"] = 999
        value["scope_sha256"] = fingerprint(value["scope"])
        row.value = value
        session.commit()
    with pytest.raises(ValueError):
        service.execute(identifier)
    assert not context["link_calls"]


def test_finalization_preserves_core_attributes_notice(context):
    from app.integrations.contracts import fingerprint
    from app.services.product_documentation import CORE_CONTENT_NOTICE
    from app.services.telegram_publication import TelegramPublicationService

    ctx = context
    service, document = setup(ctx)
    with ctx["factory"]() as session:
        product = session.scalar(select(Product).where(Product.external_id == "101:201"))
        history = session.scalar(
            select(ProductPriceHistory).where(ProductPriceHistory.product_id == product.id)
        )
        metadata = copy.deepcopy(history.evidence_metadata)
        doc = metadata["documented_identity"]
        doc["message_scope"] = "CORE_ATTRIBUTES_ONLY"
        doc["publication_name_sha256"] = fingerprint({"name": product.name})
        doc["core_attributes"] = doc.pop("facts")
        doc["core_attributes"]["kit"] = None
        history.evidence_metadata = metadata
        session.commit()
    identifier, plan = prepare(ctx, service, document)
    approve(service, identifier, plan)
    result = service.execute(identifier)
    assert result["state"] == "SUCCEEDED"
    with ctx["factory"]() as session:
        publisher = TelegramPublicationService(ctx["settings"], clock=lambda: ctx["clock"][0])
        preview = publisher.review_message(
            session, result["commercial_import"]["created_offer_ids"][0], ctx["destination_id"]
        )
        assert CORE_CONTENT_NOTICE in preview["content"]["message"]["text"]
        assert preview["content"]["message"]["photo_url"] is None


def test_expiry_after_http_preserves_result_without_link_association(context):
    ctx = context
    service, document = setup(ctx)
    identifier, plan = prepare(ctx, service, document)
    approve(service, identifier, plan)
    original = service.transport.handler

    def expire(request):
        response = original(request)
        ctx["clock"][0] += timedelta(seconds=301)
        return response

    service.transport = httpx.MockTransport(expire)
    result = service.execute(identifier)
    assert result["state"] == "UNCERTAIN"
    assert result["provider_results"][0]["credential_fingerprint"]
    assert result["provider_results"][0]["account_evidence_id"]
    assert result["provider_results"][0]["operationally_verified"] is False
    with ctx["factory"]() as session:
        assert list(session.scalars(select(AffiliateLink))) == []
    with pytest.raises(ValueError):
        service.execute(identifier)
    assert len(ctx["link_calls"]) == 1


@pytest.mark.parametrize("long_link", [None, "https://shopee.com.br/unrecognized-path"])
def test_malformed_provider_result_retained_without_approval_or_retry(context, long_link):
    ctx = context
    service, document = setup(ctx)
    identifier, plan = prepare(ctx, service, document)
    approve(service, identifier, plan)

    def malformed(request):
        ctx["link_calls"].append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "generateShortLink": {
                        "shortLink": "https://s.shopee.com.br/syntheticRecovery",
                        "longLink": long_link,
                    }
                },
                "debug": {"password": "SYNTHETIC_SECRET_MUST_NOT_BE_RECORDED"},
            },
        )

    service.transport = httpx.MockTransport(malformed)
    result = service.execute(identifier)
    assert result["state"] == "UNCERTAIN"
    observation = result["provider_http_observations"][0]
    assert observation["http_status"] == 200
    assert observation["shortLink"] == "https://s.shopee.com.br/syntheticRecovery"
    assert observation["verification_status"] == "UNVERIFIED_PROVIDER_RESPONSE"
    assert "SYNTHETIC_SECRET_MUST_NOT_BE_RECORDED" not in str(result)
    with ctx["factory"]() as session:
        assert list(session.scalars(select(AffiliateLink))) == []
    with pytest.raises(ValueError):
        service.execute(identifier)
    assert len(ctx["link_calls"]) == 1

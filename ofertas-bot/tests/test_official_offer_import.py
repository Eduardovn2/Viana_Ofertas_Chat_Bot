"""Confirmed local CSVs feed the existing offer/outbox paths; no real provider I/O."""

import csv
import hashlib
import io
from datetime import timedelta

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_proportional_identity as proportional
from sqlalchemy import func, select

from app.models import AppSetting, Offer, Product, ProductPriceHistory
from app.services.manual_offers import read_manual_offer
from app.services.official_offer_import import PREFIX, OfficialExportInput, process_export

base_context = real.base_context
context = real.context
no_network = real.no_network
application = real.application
client = real.client


def document(ctx, *, listing=False):
    if listing:
        proportional.listing_offer(ctx)
    else:
        offline.listings(ctx, [1])
    with ctx["factory"]() as session:
        product = session.scalar(select(Product).where(Product.external_id == "101:201"))
        row = dict(
            shop_id="101",
            item_id="201",
            variant_id=product.variant_id,
            name=product.name,
            original_url=product.original_url,
            public_price="100.00",
            availability="AVAILABLE",
            price_access="PUBLIC",
            currency="BRL",
            observed_at=ctx["clock"][0].isoformat(),
            all_options_same_public_terms="yes",
            all_options_available="yes",
        )
    data = dict(
        source_url="https://affiliate.shopee.com.br/",
        source_reference="FICTICIO export official account document",
        account_reference="SYNTHETIC_ACCOUNT_NOT_A_REAL_SOURCE",
        column_semantics_reference="FICTICIO column dictionary explicitly reviewed",
        exported_at=ctx["clock"][0],
        actor="synthetic_export_reviewer",
        columns={key: key for key in row},
        available_value="AVAILABLE",
        public_value="PUBLIC",
        all_options_true_value="yes",
        confirmed_official_document=True,
        confirmed_column_semantics=True,
        confirmed_commercial_import=True,
    )
    return data, row


def encoded(data, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    content = output.getvalue()
    return dict(
        data, csv_content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest()
    )


def run(ctx, payload, *, persist=True):
    with ctx["factory"]() as session:
        result = process_export(
            session, ctx["settings"], payload, persist=persist, now=ctx["clock"][0]
        )
        session.commit()
        return result


def count(ctx, model):
    with ctx["factory"]() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_preview_never_creates_offers_or_verifications(context):
    data, row = document(context)
    baseline = count(context, Offer), count(context, ProductPriceHistory)
    result = run(context, encoded(data, [row]), persist=False)
    assert result["rows"][0]["state"] == "READY"
    assert baseline == (count(context, Offer), count(context, ProductPriceHistory))
    with context["factory"]() as session:
        assert not session.scalar(select(AppSetting).where(AppSetting.key.startswith(PREFIX)))


def test_import_reuses_valid_variant_document_without_repeated_confirmation(context):
    data, row = document(context)
    result = run(context, encoded(data, [row]))
    assert len(result["created_offer_ids"]) == 1
    with context["factory"]() as session:
        bundle = read_manual_offer(
            session, result["created_offer_ids"][0], context["settings"], context["clock"][0]
        )
        assert bundle.history.evidence_metadata["official_export"]["row_sha256"]
        assert (
            bundle.history.evidence_metadata["documented_identity"]["source"] == "MANUAL_DOCUMENTED"
        )
        assert bundle.offer.coupon_id is None and bundle.offer.previous_price is None


def test_listing_export_can_refresh_only_explicit_all_option_conditions(context):
    data, row = document(context, listing=True)
    context["clock"][0] += timedelta(seconds=10)
    row.update(public_price="105.00", observed_at=context["clock"][0].isoformat())
    data["exported_at"] = context["clock"][0]
    result = run(context, encoded(data, [row]))
    assert len(result["created_offer_ids"]) == 1
    with context["factory"]() as session:
        offer = session.get(Offer, result["created_offer_ids"][0])
        proof = session.get(ProductPriceHistory, offer.price_history_id).evidence_metadata[
            "documented_identity"
        ]
        assert (
            proof["source"] == "OFFICIAL_EXPORT" and proof["all_options_public_price"] == "105.00"
        )
        assert proof["variant_id"] == "" and proof["facts"] is None


def test_same_file_is_idempotent_and_does_not_refresh_expired_evidence(context):
    data, row = document(context)
    payload = encoded(data, [row])
    first = run(context, payload)
    baseline = count(context, Offer)
    assert run(context, payload)["currently_valid_offer_ids"] == first["created_offer_ids"]
    context["clock"][0] += timedelta(seconds=601)
    repeated = run(context, payload)
    assert repeated["replay"] and repeated["currently_valid_offer_ids"] == []
    assert count(context, Offer) == baseline


@pytest.mark.parametrize(
    "change",
    [
        "missing_availability",
        "missing_public",
        "missing_time",
        "ambiguous_price",
        "unknown_stock",
        "old_time",
        "future_time",
        "wrong_url",
        "currency",
        "unknown_identity",
        "wrong_variant",
    ],
)
def test_incomplete_or_conflicting_export_rows_never_create_verified_offers(context, change):
    data, row = document(context)
    if change.startswith("missing_"):
        field = dict(
            missing_availability="availability",
            missing_public="price_access",
            missing_time="observed_at",
        )[change]
        del data["columns"][field]
    else:
        updates = dict(
            ambiguous_price={"public_price": "99-199"},
            unknown_stock={"availability": "UNKNOWN"},
            old_time={"observed_at": (context["clock"][0] - timedelta(seconds=601)).isoformat()},
            future_time={"observed_at": (context["clock"][0] + timedelta(seconds=1)).isoformat()},
            wrong_url={"original_url": "https://shopee.com.br/product/1/2"},
            currency={"currency": "USD"},
            unknown_identity={"shop_id": "999"},
            wrong_variant={"variant_id": "not-documented"},
        )
        row.update(updates[change])
    baseline = count(context, Offer)
    result = run(context, encoded(data, [row]))
    assert result["blocked"] == 1 and result["created_offer_ids"] == []
    assert count(context, Offer) == baseline


@pytest.mark.parametrize(
    "field",
    ["confirmed_official_document", "confirmed_column_semantics", "confirmed_commercial_import"],
)
def test_import_requires_explicit_batch_document_and_commercial_authorization(context, field):
    data, row = document(context)
    data[field] = False
    with pytest.raises(ValueError):
        run(context, encoded(data, [row]))


def test_all_option_refresh_requires_actual_coverage_columns(context):
    data, row = document(context, listing=True)
    row["all_options_available"] = "unknown"
    assert (
        run(context, encoded(data, [row]))["rows"][0]["reason"] == "ALL_OPTIONS_COVERAGE_REQUIRED"
    )


def test_duplicate_conflicting_rows_are_both_blocked(context):
    data, row = document(context)
    result = run(context, encoded(data, [row, dict(row, public_price="99")]))
    assert result["blocked"] == 2 and result["created_offer_ids"] == []


def test_fingerprint_origin_and_mapping_changes_are_rejected(context):
    data, row = document(context)
    payload = encoded(data, [row])
    with pytest.raises(ValueError):
        run(context, dict(payload, content_sha256="0" * 64))
    with pytest.raises(ValueError):
        OfficialExportInput.model_validate(
            dict(payload, source_url="https://affiliate.shopee.com.br.evil.test/")
        )
    run(context, payload)
    with pytest.raises(ValueError):
        run(context, dict(payload, available_value="UNKNOWN"))


def test_imported_offer_enters_existing_pilot_without_new_query_or_individual_approval(context):
    ctx = context
    real.authorized(ctx, max_messages=1)
    data, row = document(ctx)
    result = run(ctx, encoded(data, [row]))
    report = ctx["pilot"].tick(ctx["pilot_id"])
    assert report["counts"]["published"] == 1
    assert report["counts"]["queries_reserved"] == 0 and ctx["calls"] == 0
    assert report["queue"][0]["offer_id"] == result["created_offer_ids"][0]


def test_prepared_pilot_cannot_consume_imports(context):
    ctx = context
    real.prepared(ctx)
    data, row = document(ctx)
    run(ctx, encoded(data, [row]))
    ctx["pilot"].enqueue_documented_imports(ctx["pilot_id"])
    report = ctx["pilot"].report(ctx["pilot_id"])
    assert report["state"] == "PREPARED" and report["queue"] == []
    assert ctx["calls"] == 0 and ctx["telegram_calls"] == []


def test_import_does_not_expand_search_scope(context):
    ctx = context
    real.authorized(ctx, terms=["Creatina monohidratada 250g"])
    data, row = document(ctx)
    run(ctx, encoded(data, [row]))
    ctx["pilot"].enqueue_documented_imports(ctx["pilot_id"])
    assert ctx["pilot"].report(ctx["pilot_id"])["queue"] == []


def test_new_manual_identity_document_cannot_bypass_confirmation(context):
    data, row = document(context)
    with context["factory"]() as session:
        history = session.scalar(
            select(ProductPriceHistory).order_by(ProductPriceHistory.id.desc())
        )
        product = session.get(Product, history.product_id)
        data["identity_documents"] = {
            str(product.id): history.evidence_metadata["documented_identity"]
        }
    result = run(context, encoded(data, [row]))
    assert result["rows"][0]["reason"] == "NEW_IDENTITY_DOCUMENT_CONFIRMATION_REQUIRED"


def test_import_endpoints_require_admin(client):
    assert client.post("/api/v1/manual-offers/official-exports/preview", json={}).status_code == 401
    assert client.post("/api/v1/manual-offers/official-exports/import", json={}).status_code == 401


@pytest.mark.parametrize("change", ["availability", "price", "public"])
def test_newer_negative_or_inconclusive_export_blocks_prior_offer_without_rewriting_history(
    context, change
):
    from app.integrations.errors import IntegrationError

    data, row = document(context)
    first = run(context, encoded(data, [row]))
    baseline = count(context, ProductPriceHistory)
    context["clock"][0] += timedelta(seconds=10)
    data["exported_at"] = context["clock"][0]
    row["observed_at"] = context["clock"][0].isoformat()
    row.update(
        {
            "availability": {"availability": "UNKNOWN"},
            "price": {"public_price": "99-199"},
            "public": {"price_access": "UNKNOWN"},
        }[change]
    )
    assert run(context, encoded(data, [row]))["created_offer_ids"] == []
    assert count(context, ProductPriceHistory) == baseline
    with context["factory"]() as session, pytest.raises(IntegrationError):
        read_manual_offer(
            session, first["created_offer_ids"][0], context["settings"], context["clock"][0]
        )


def test_unknown_status_cannot_be_mapped_to_available_or_public(context):
    data, row = document(context)
    with pytest.raises(ValueError):
        OfficialExportInput.model_validate(dict(encoded(data, [row]), available_value="UNKNOWN"))


def test_conflicting_url_variant_is_rejected_even_when_listing_ids_match(context):
    data, row = document(context)
    row["original_url"] += "?model_id=OTHER_OPTION"
    result = run(context, encoded(data, [row]))
    assert result["created_offer_ids"] == [] and result["blocked"] == 1


def test_authenticated_api_imports_through_existing_manual_offer_endpoint(context, monkeypatch):
    from fastapi.encoders import jsonable_encoder
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.api import manual_offers as api
    from app.main import create_app

    data, row = document(context)
    app = create_app(
        context["settings"].model_copy(
            update={"admin_api_key": SecretStr("synthetic-admin-key-only")}
        ),
        background_monitoring_allowed=False,
    )
    app.state.session_factory = context["factory"]
    original = api.process_export
    monkeypatch.setattr(
        api,
        "process_export",
        lambda *args, **kwargs: original(*args, **kwargs, now=context["clock"][0]),
    )
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        headers = {"X-API-Key": "synthetic-admin-key-only"}
        payload = jsonable_encoder(encoded(data, [row]))
        base = "/api/v1/manual-offers/official-exports/"
        assert (
            client.post(base + "preview", json=payload, headers=headers).json()["rows"][0]["state"]
            == "READY"
        )
        response = client.post(base + "import", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        offer_id = response.json()["created_offer_ids"][0]
        assert client.get(f"/api/v1/manual-offers/{offer_id}", headers=headers).status_code == 200

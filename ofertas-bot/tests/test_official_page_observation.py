"""A reviewed page observation is distinct from a provider-generated export."""

import pytest
import test_official_offer_import as exports

from app.services.official_offer_import import OfficialExportInput, process_export

base_context = exports.base_context
context = exports.context
no_network = exports.no_network


def test_reviewed_page_observation_reuses_import_without_claiming_export(context):
    data, row = exports.document(context, listing=True)
    data.update(
        document_kind="OFFICIAL_PAGE_OBSERVATION",
        observation_source_sha256="a" * 64,
        source_url=row["original_url"],
    )
    result = exports.run(context, exports.encoded(data, [row]))
    assert result["created_offer_ids"]
    from app.models import Offer, ProductPriceHistory

    with context["factory"]() as session:
        offer = session.get(Offer, result["created_offer_ids"][0])
        provenance = session.get(ProductPriceHistory, offer.price_history_id).evidence_metadata[
            "official_export"
        ]
        assert provenance["document_kind"] == "OFFICIAL_PAGE_OBSERVATION"
        assert provenance["observation_source_sha256"] == "a" * 64


@pytest.mark.parametrize(
    "change",
    [{"observation_source_sha256": None}, {"source_url": "https://affiliate.shopee.com.br/"}],
)
def test_page_provenance_is_required(context, change):
    data, row = exports.document(context)
    data.update(
        document_kind="OFFICIAL_PAGE_OBSERVATION",
        observation_source_sha256="a" * 64,
        source_url=row["original_url"],
    )
    data.update(change)
    with pytest.raises(ValueError):
        OfficialExportInput.model_validate(exports.encoded(data, [row]))


def test_page_cannot_supply_another_listing_or_multiple_rows(context):
    data, row = exports.document(context)
    data.update(
        document_kind="OFFICIAL_PAGE_OBSERVATION",
        observation_source_sha256="a" * 64,
        source_url="https://shopee.com.br/product/999/999",
    )
    with context["factory"]() as session:
        result = process_export(
            session, context["settings"], exports.encoded(data, [row]), now=context["clock"][0]
        )
        assert result["rows"][0]["reason"] == "OBSERVED_PAGE_LISTING_MISMATCH"
        with pytest.raises(ValueError):
            process_export(
                session,
                context["settings"],
                exports.encoded(data, [row, row]),
                now=context["clock"][0],
            )

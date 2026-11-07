"""The official-export path uses migrated isolated PostgreSQL, never operational data."""

from concurrent.futures import ThreadPoolExecutor

import pytest
import test_official_offer_import as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "case",
    [
        "preview_never_creates_offers_or_verifications",
        "import_reuses_valid_variant_document_without_repeated_confirmation",
        "listing_export_can_refresh_only_explicit_all_option_conditions",
        "same_file_is_idempotent_and_does_not_refresh_expired_evidence",
        "all_option_refresh_requires_actual_coverage_columns",
        "duplicate_conflicting_rows_are_both_blocked",
        "fingerprint_origin_and_mapping_changes_are_rejected",
        "imported_offer_enters_existing_pilot_without_new_query_or_individual_approval",
        "prepared_pilot_cannot_consume_imports",
        "import_does_not_expand_search_scope",
        "new_manual_identity_document_cannot_bypass_confirmation",
        "conflicting_url_variant_is_rejected_even_when_listing_ids_match",
    ],
)
def test_postgres_official_import(context, case):
    getattr(cases, "test_" + case)(context)


def test_concurrent_same_file_never_duplicates_commercial_evidence(context):
    data, row = cases.document(context)
    payload = cases.encoded(data, [row])
    baseline = cases.count(context, cases.Offer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cases.run(context, payload), range(2)))
    assert results[0]["created_offer_ids"] == results[1]["created_offer_ids"]
    assert cases.count(context, cases.Offer) == baseline + 1


@pytest.mark.parametrize("change", ["availability", "price", "public"])
def test_postgres_newer_export_invalidates_prior_commercial_evidence(context, change):
    cases.test_newer_negative_or_inconclusive_export_blocks_prior_offer_without_rewriting_history(
        context, change
    )

"""Proportional identity through existing services on isolated PostgreSQL."""

import pytest
import test_postgres_auto_pilot_real as pg
import test_proportional_identity as cases

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "case",
    [
        "listing_without_sku_can_send_only_with_full_all_options_evidence",
        "listing_name_change_invalidates_document_and_preview",
        "listing_keys_do_not_assert_equivalence_between_sellers",
        "documented_api_title_alias_is_accepted_but_undocumented_is_blocked",
        "undocumented_alias_cannot_send",
        "partial_candidate_is_never_imported_as_verified_offer",
    ],
)
def test_postgres_proportional_identity(context, case):
    getattr(cases, "test_" + case)(context)

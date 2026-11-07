"""Commercial adapter regressions in migrated, isolated PostgreSQL schemas."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_commercial_handoff as contracts
import test_shopee_multistore as multistore

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = multistore.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["transport"].close()


@pytest.mark.parametrize(
    "name",
    [
        "preview_is_read_only_informative_and_reuses_identity_evidence",
        "partial_incompatible_and_nonselected_candidates_cannot_be_registered",
        "existing_manual_offer_reuses_commercial_evidence_without_publication",
        "existing_explicit_approval_flow_revalidates_multistore_evidence",
        "stale_context_and_wrong_product_block_before_history_write",
        "previous_real_response_remains_inconclusive_without_new_calls",
        "tied_minimum_prices_keep_both_administrative_previews",
        "mock_source_cannot_enter_commercial_workflow_outside_test_environment",
    ],
)
def test_postgres_commercial_handoff(context, name):
    getattr(contracts, "test_" + name)(context)


@pytest.mark.parametrize("change", ["observation", "draft", "expired", "variant"])
def test_postgres_invalidated_evidence(context, change):
    contracts.test_changed_evidence_blocks_existing_offer_and_publication_context(context, change)

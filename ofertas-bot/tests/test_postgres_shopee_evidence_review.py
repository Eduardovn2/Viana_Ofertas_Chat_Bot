"""Evidence drafts and confirmation in migrated PostgreSQL UUID-isolated schemas."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_evidence_review as contracts

categories_postgres = pg.categories_postgres
no_network = collection.no_network
pytestmark = pytest.mark.postgres


@pytest.fixture
def context(categories_postgres):
    engine, url = categories_postgres
    ctx = contracts.setup(collection.setup(engine, url.render_as_string(hide_password=False)))
    yield ctx
    ctx["transport"].close()


@pytest.mark.parametrize(
    "name",
    [
        "partial_sources_merge_with_per_attribute_provenance",
        "confirmed_reviews_reuse_equivalence_gate_without_publication",
        "draft_never_promotes_and_authorization_is_preserved",
        "editing_documented_review_withdraws_active_proof_and_preserves_archive",
        "stale_revision_and_fingerprint_are_rejected",
        "changed_saved_observation_invalidates_review",
        "new_keyword_mock_execution_preserves_query_and_single_call",
    ],
)
def test_postgres_evidence_review(context, name):
    getattr(contracts, "test_" + name)(context)

"""Consolidation and existing confirmed evidence in migrated PostgreSQL test schemas."""

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_multistore as multistore
import test_shopee_opportunities as contracts

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
        "draft_fields_are_pending_confirmation_not_automatic_facts",
        "single_documented_seller_waits_for_peer_not_manual_reconfirmation",
        "changed_observation_and_stale_reviews_return_to_triage",
        "documented_incompatible_product_is_not_sent_for_manual_review",
        "handoff_remains_announced_only_and_cannot_authorize_publication",
    ],
)
def test_postgres_opportunities(context, name):
    getattr(contracts, "test_" + name)(context)


def test_postgres_automatic_evidence_reuse(context, monkeypatch):
    contracts.test_documented_reviews_are_reused_without_new_confirmation_or_offer_creation(
        context, monkeypatch
    )

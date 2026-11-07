"""Defined product flow in existing migrated UUID-isolated PostgreSQL test schemas."""

import inspect

import pytest
import test_postgres_categories as pg
import test_shopee_collection as collection
import test_shopee_defined_product as contracts

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
        "defined_ranking_ties_and_deduplication",
        "partial_target_does_not_merge_different_kits_or_variants",
        "documented_target_conflicts_are_explained_even_with_incomplete_title",
        "target_is_not_evidence_and_no_unknown_variant_is_filled",
        "different_price_conditions_are_separate_groups",
        "range_missing_ratings_and_quote_fingerprints",
        "review_is_read_only_and_cannot_call_connector",
        "replay_rejects_duplicate_identity",
        "replay_cannot_rank_a_forged_single_price_over_a_range",
        "defined_product_search_uses_single_existing_discovery",
    ],
)
def test_postgres_defined_product(context, monkeypatch, name):
    function = getattr(contracts, "test_" + name)
    if "monkeypatch" in inspect.signature(function).parameters:
        function(context, monkeypatch)
    else:
        function(context)

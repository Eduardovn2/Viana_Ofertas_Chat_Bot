"""Standardized product comparison under the existing isolated PostgreSQL fixtures."""

import pytest
import test_postgres_categories as pg
import test_shopee_classification as contracts
import test_shopee_collection as collection

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
        "creatine_proven_groups_do_not_mix_weight_or_edition",
        "title_only_differences_never_prove_equivalence_or_incompatibility",
        "store_alternatives_and_unproven_cheapest_are_preserved",
        "requested_content_proven_mismatch_is_separate_from_unknown",
        "missing_documented_mass_cannot_rank_creatine",
    ],
)
def test_postgres_classification(context, name):
    getattr(contracts, "test_" + name)(context)

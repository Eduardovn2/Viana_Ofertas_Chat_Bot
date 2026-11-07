"""Focused contracts in an isolated PostgreSQL schema, never operational data."""

import pytest
import test_account_evidence_reuse as contracts
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "name",
    [
        "account_proof_idempotent_expiry_rotation_and_no_secrets",
        "same_account_reused_but_legacy_link_not_inferred",
        "limited_message_is_text_only_explicit_and_not_equivalence",
    ],
)
def test_postgres_reusable_evidence(context, name):
    getattr(contracts, "test_" + name)(context)

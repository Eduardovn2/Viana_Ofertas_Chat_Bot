"""Phase-4 contracts in UUID schemas on an explicitly configured *_test database.

No Telegram client, real token, profile or network publisher is used here.
The shared fixture drops only the schema it creates for this test.
"""

from datetime import UTC, datetime

import pytest
import test_migrations as contracts
import test_postgres_categories as postgres_fixtures
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.models import Base, ManualOfferDetails, TelegramPublication

categories_postgres = postgres_fixtures.categories_postgres


@pytest.mark.postgres
def test_postgres_telegram_schema_manual_evidence_and_approval_slots(categories_postgres):
    engine, _ = categories_postgres
    inspector = inspect(engine)
    assert {"manual_offer_details", "telegram_publications"} <= set(inspector.get_table_names())
    assert inspector.get_pk_constraint("manual_offer_details")["constrained_columns"] == [
        "offer_id"
    ]
    assert ("product_id", "destination_id", "active_slot") in {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("telegram_publications")
    }
    assert all(
        key["options"]["ondelete"] == "RESTRICT"
        for table in ("manual_offer_details", "telegram_publications")
        for key in inspector.get_foreign_keys(table)
    )
    assert "preview_payload" in {
        column["name"] for column in inspector.get_columns("telegram_publications")
    }
    assert (
        str(
            next(
                column["type"]
                for column in inspector.get_columns("telegram_publications")
                if column["name"] == "preview_payload"
            )
        )
        == "JSONB"
    )
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, Base.metadata) == []
    ids = contracts._seed_telegram_rows(engine)
    contracts._assert_telegram_active_slot_lifecycle(engine, ids)
    with Session(engine) as session:
        sent = (
            session.query(TelegramPublication)
            .filter(TelegramPublication.message_id == "synthetic-message-1")
            .one()
        )
        assert sent.preview_payload["evidence"]["history_id"] == ids["history_id"]
        assert sent.resolution_evidence.startswith("SYNTHETIC auditor")
        session.add(
            ManualOfferDetails(
                offer_id=ids["offer_id"],
                verified_by="duplicate",
                verified_at=datetime(2026, 10, 18, tzinfo=UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.postgres
def test_postgres_telegram_checks_fks_and_non_destructive_downgrade(categories_postgres):
    engine, _ = categories_postgres
    ids = contracts._seed_telegram_rows(engine)
    with engine.begin() as connection:
        for status, active_slot in (
            ("UNCERTAIN", None),
            ("APPROVED", None),
            ("SENT", 1),
            ("UNKNOWN", 1),
        ):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO telegram_publications "
                        "(offer_id, product_id, destination_id, status, active_slot, preview_hash, created_at, updated_at) "
                        "VALUES (:offer, :product, :destination, :status, :slot, :hash, :now, :now)"
                    ),
                    {
                        "offer": ids["offer_id"],
                        "product": ids["product_id"],
                        "destination": ids["destination_id"],
                        "status": status,
                        "slot": active_slot,
                        "hash": "a" * 64,
                        "now": datetime(2026, 10, 18, tzinfo=UTC),
                    },
                )
    with Session(engine) as session:
        session.add(contracts._telegram_entry(ids))
        session.commit()
    with engine.begin() as connection:
        for table, key in (
            ("products", "product_id"),
            ("offers", "offer_id"),
            ("publication_destinations", "destination_id"),
        ):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(f"DELETE FROM {table} WHERE id=:id"), {"id": ids[key]})
    config = Config(str(contracts.PROJECT_ROOT / "alembic.ini"))
    with pytest.raises(RuntimeError, match="without data loss"):
        command.downgrade(config, "0004_marketplace_provenance")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0005_telegram_outbox"
        )
        assert connection.scalar(text("SELECT count(*) FROM telegram_publications")) == 1
        assert connection.scalar(text("SELECT count(*) FROM manual_offer_details")) == 1

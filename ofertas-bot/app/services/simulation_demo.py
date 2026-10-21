"""Bounded APScheduler demonstration in a NEW retained PostgreSQL test schema only."""

import json
import logging
import os
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from alembic import command
from app.core.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.jobs.simulation import SimulationMonitoringRuntime
from app.models import (
    AffiliateLink,
    Category,
    CategoryMode,
    DestinationCategory,
    DestinationChannel,
    Platform,
    Product,
    PublicationDestination,
    SimulationAudit,
    TelegramSimulation,
)
from app.services.categories import CategoryService
from app.services.simulation_safety import configure_policy, enroll_product

SCENARIO = (
    ("PlayStation 5", "3000.00", "2700.00"),
    ("Smartphone iPhone 15", "4000.00", "3600.00"),
    ("Ferramentas furadeira", "500.00", "450.00"),
    ("PlayStation 4", "1500.00", "1300.00"),
    ("Sofá casa", "1000.00", "800.00"),
    ("Mouse sem queda", "100.00", "100.00"),
)


def seed_demo(engine, settings):
    factory = build_session_factory(engine)
    with factory() as session:
        CategoryService().seed(session)
        destination = PublicationDestination(
            name="[SIMULATED] Telegram demonstração",
            external_id="simulation:phase47",
            channel=DestinationChannel.SIMULATED,
            category_mode=CategoryMode.SELECTED,
            enabled=True,
            manual_approval_required=True,
            min_interval_seconds=1,
            max_messages_per_hour=5,
        )
        session.add(destination)
        session.flush()
        for category in session.scalars(
            select(Category).where(
                Category.slug.in_(["games", "tecnologia", "materiais-ferramentas"])
            )
        ):
            session.add(DestinationCategory(destination_id=destination.id, category_id=category.id))
        for index, (name, baseline, current) in enumerate(SCENARIO):
            product = Product(
                name="[SIMULATED] " + name,
                platform=Platform.MERCADO_LIVRE,
                external_id=f"SIMULATED{index}",
                original_url=f"https://example.test/fictitious/{index}",
                classification_metadata={"simulation_only": True},
                monitoring_enabled=True,
                priority=True,
            )
            product.affiliate_link = AffiliateLink(
                url="https://meli.la/SIMULATED_ONLY", source="manual_official"
            )
            session.add(product)
            session.flush()
            enroll_product(session, settings, product.id, {"prices": [baseline, current]})
        configure_policy(
            session,
            settings,
            destination,
            {
                "enabled": True,
                "automatic_approval": True,
                "terms": ["PlayStation 5", "iPhone 15", "ferramentas"],
            },
        )
        session.commit()
    return factory


def main():
    original = os.environ.get("DATABASE_URL")
    engine = admin = runtime = None
    try:
        local = Settings()
        if (
            local.telegram_publisher_enabled
            or local.telegram_real_sends_authorized
            or local.mercado_livre_enabled
            or local.shopee_enabled
        ):
            raise ValueError("Real controls must stay disabled")
        url = make_url(local.database_url)
        if url.drivername != "postgresql+psycopg":
            raise ValueError("PostgreSQL required for isolated demonstration")
        schema = "ofertas_bot_simulation_test_" + uuid4().hex
        url = url.set(database="ofertas_bot_test")
        admin = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        isolated = url.update_query_dict({"options": f"-csearch_path={schema}"})
        os.environ["DATABASE_URL"] = isolated.render_as_string(hide_password=False)
        get_settings.cache_clear()
        command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
        command.check(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")))
        logging.disable(logging.CRITICAL)
        settings = Settings(
            _env_file=None,
            app_env="development",
            database_url=isolated.render_as_string(hide_password=False),
            scheduler_enabled=True,
            simulation_automation_enabled=True,
            priority_interval_seconds=1,
            general_interval_seconds=1,
            provider_min_interval_seconds=0,
            scheduler_tick_seconds=1,
            telegram_publisher_enabled=False,
            telegram_real_sends_authorized=False,
            mercado_livre_enabled=False,
            shopee_enabled=False,
            telegram_bot_token=None,
            telegram_allowed_chat_ids=(),
        )
        engine = build_engine(settings.database_url)
        factory = seed_demo(engine, settings)
        runtime = SimulationMonitoringRuntime(factory, settings)
        started = runtime.start()
        if not started["running"]:
            raise ValueError(
                "Scheduler could not start; no synthetic manual success is substituted"
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if runtime.pipeline.metrics()["simulated_products"] == 3:
                break
            Event().wait(0.2)
        runtime.pause()
        runtime.shutdown()
        metrics = runtime.pipeline.metrics()
        if metrics["simulated_products"] != 3:
            raise ValueError("Demonstration incomplete")
        with factory() as session:
            first = session.scalar(
                select(TelegramSimulation.id)
                .where(TelegramSimulation.status == "SIMULATED")
                .order_by(TelegramSimulation.id)
            )
            automatic = list(
                session.scalars(
                    select(SimulationAudit)
                    .where(SimulationAudit.event == "AUTO_APPROVED_SIMULATED")
                    .order_by(SimulationAudit.id)
                )
            )
        before = runtime.pipeline.delivery.calls
        replay_status = runtime.pipeline.dispatch(first)
        after = runtime.pipeline.delivery.calls
        metrics = runtime.pipeline.metrics()
        report = {
            "mode": "SIMULATED",
            "database": "ofertas_bot_test",
            "retained_schema": schema,
            "scheduler": "existing APScheduler, actual bounded background execution",
            "scheduler_stopped": not runtime.status()["running"],
            "products": [
                {"name": "[SIMULATED] " + name, "baseline": baseline, "current": current}
                for name, baseline, current in SCENARIO
            ],
            **metrics,
            "duplicate_dispatch": {
                "result": replay_status,
                "mock_calls_before": before,
                "mock_calls_after": after,
                "extra_calls": after - before,
            },
            "auto_approval_audit_ids": [event.id for event in automatic],
            "real_publisher_enabled": False,
            "real_sends_authorized": False,
            "no_live_telegram_calls": True,
        }
        output = Path(__file__).resolve().parents[2] / "docs" / "DEMONSTRACAO_FASE_4_7.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))
    except Exception as error:
        print("DEMO_BLOCKED:", type(error).__name__)
        raise SystemExit(1) from None
    finally:
        if runtime is not None:
            runtime.pause()
            runtime.shutdown()
        if engine is not None:
            engine.dispose()
        if admin is not None:
            admin.dispose()
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        get_settings.cache_clear()


if __name__ == "__main__":
    main()

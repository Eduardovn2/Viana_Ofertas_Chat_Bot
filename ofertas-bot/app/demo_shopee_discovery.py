"""Bounded offline APScheduler demonstration; synthetic credentials and a new SQLite file.

Run: python -m app.demo_shopee_discovery
No .env loading, network transport, Telegram publisher or affiliate mutation is used.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

import httpx
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import SecretStr
from sqlalchemy import func, select

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.http import JsonTransport
from app.integrations.registry import ConnectorRegistry
from app.integrations.shopee import ShopeeConnector
from app.jobs.runtime import MonitoringRuntime
from app.models import (
    Base,
    Category,
    CategoryMode,
    DestinationChannel,
    Platform,
    Product,
    ProductPriceHistory,
    PublicationDestination,
    PublicationQueueItem,
    ShopeeCollectionCandidate,
    ShopeeDiscoveryFinding,
    ShopeeDiscoveryProduct,
)
from app.services.categories import CATALOG, CategoryService
from app.services.shopee_discovery import JOB_IDS, ShopeeDiscoveryService

SAMPLES = (
    ("organizador", "generic"),
    ("furadeira", "tool"),
    ("geladeira", "generic"),
    ("camiseta", "generic"),
    ("brinquedo", "generic"),
    ("smartphone iPhone 15", "phone"),
    ("Console PlayStation 5", "console"),
    ("shampoo", "generic"),
    ("objeto XYZ", "generic"),
)


def run():
    directory = Path(".runtime")
    directory.mkdir(exist_ok=True)
    database = directory / f"discovery-{uuid4().hex}.simulation.db"
    settings = Settings(
        _env_file=None,
        app_env="development",
        database_url="sqlite:///" + database.as_posix(),
        scheduler_enabled=False,
        price_provider="simulated",
        shopee_enabled=False,
        mercado_livre_enabled=False,
        telegram_publisher_enabled=False,
        telegram_real_sends_authorized=False,
        simulation_automation_enabled=False,
        shopee_app_id=SecretStr("SYNTHETIC_OFFLINE"),
        shopee_secret=SecretStr("synthetic-secret"),
    )
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    clock = [datetime(2026, 10, 21, 13, tzinfo=UTC)]
    state = {"discovery": 0, "monitor": 0, "evaluation": 0, "mock_requests": 0, "errors": []}
    done, lock = Event(), Lock()

    def node(index, name, price="100.00"):
        return {
            "shopId": 7001,
            "itemId": index,
            "productName": "[FICTICIO] " + name,
            "productLink": f"https://shopee.com.br/product/7001/{index}",
            "price": price,
            "priceMin": price,
            "priceMax": price,
            "productCatIds": [100535, 0],
            "commissionRate": "0.05",
            "commission": "5",
        }

    def handler(request):
        query = json.loads(request.content)["query"]
        assert "mutation" not in query and "productOfferV2(" in query
        state["mock_requests"] += 1
        page = int(re.search(r"page:\s*(\d+)", query).group(1))
        item = re.search(r"itemId:\s*(\d+)", query)
        if item:
            identity = int(item.group(1))
            index = (identity - 1) // 10
            nodes, has_next = [node(identity, SAMPLES[index][0], "90.00")], False
        else:
            keyword = re.search(r'keyword:\s*("(?:[^"\\]|\\.)*")', query)
            term = json.loads(keyword.group(1))
            index = [sample[0] for sample in SAMPLES].index(term)
            identity = index * 10 + 1
            nodes = (
                [node(identity, term), node(identity + 1, "item incompativel")]
                if page == 1
                else [node(identity, term), node(identity + 2, term)]
            )
            has_next = page == 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "productOfferV2": {
                        "nodes": nodes,
                        "pageInfo": {"page": page, "hasNextPage": has_next},
                    }
                }
            },
        )

    transport = JsonTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
        clock=lambda: clock[0],
        min_interval=0,
    )
    connector = ShopeeConnector(
        transport,
        app_id=settings.shopee_app_id,
        secret=settings.shopee_secret,
        enabled=True,
        clock=lambda: clock[0],
    )
    registry = ConnectorRegistry(settings, session_factory=factory, clock=lambda: clock[0])
    registry._connectors[Platform.SHOPEE] = connector
    service = ShopeeDiscoveryService(factory, settings, registry=registry, clock=lambda: clock[0])
    runtime = MonitoringRuntime(factory, settings, clock=lambda: clock[0])
    with factory() as session:
        CategoryService().seed(session)
        destination = PublicationDestination(
            name="[FICTICIO] laboratorio",
            channel=DestinationChannel.SIMULATED,
            external_id="discovery-laboratory",
            enabled=True,
            manual_approval_required=True,
            category_mode=CategoryMode.ALL,
        )
        session.add(destination)
        session.commit()
        destination_id = destination.id
        categories = {c.slug: c.id for c in session.scalars(select(Category))}
    for (slug, _), (query, intent) in zip(CATALOG, SAMPLES, strict=True):
        service.configure(
            categories[slug],
            {
                "enabled": True,
                "searches": [{"query": query, "intent": intent}],
                "page_size": 2,
                "max_pages": 2,
                "destinations": [destination_id],
            },
            "offline demonstration",
        )
    service.prepare_schedule(runtime, offline=True)

    def tick(operation):
        if not lock.acquire(blocking=False):
            return
        try:
            expected = (
                "DISCOVER"
                if state["discovery"] < 9
                else "MONITOR"
                if state["monitor"] < 18
                else "EVALUATE"
            )
            if operation != expected or done.is_set():
                return
            result = service.execute(uuid4(), operation)
            if result["status"] != "SUCCEEDED":
                state["errors"].append(result)
                done.set()
                return
            key = {"DISCOVER": "discovery", "MONITOR": "monitor", "EVALUATE": "evaluation"}[
                operation
            ]
            state[key] += 1
            if operation == "EVALUATE":
                done.set()
            else:
                clock[0] += timedelta(seconds=3601)
        except Exception as error:
            state["errors"].append(type(error).__name__)
            done.set()
        finally:
            lock.release()

    for job_id, op in zip(JOB_IDS, ("DISCOVER", "MONITOR", "EVALUATE"), strict=True):
        runtime.prepare_paused_job(job_id, lambda op=op: tick(op), 900)
    try:
        runtime.start()
        if runtime._scheduler is None:
            raise RuntimeError("Demonstration requires a separate CLI, outside pytest.")
        for job_id in JOB_IDS:
            runtime._scheduler.modify_job(
                job_id, trigger=IntervalTrigger(seconds=0.5), next_run_time=datetime.now(UTC)
            )
        if not done.wait(45):
            raise RuntimeError("Offline demonstration deadline exceeded.")
    finally:
        runtime.shutdown()
        transport.close()
    with factory() as session:
        counts = {
            key: session.scalar(
                select(func.count())
                .select_from(ShopeeDiscoveryFinding)
                .where(ShopeeDiscoveryFinding.disposition == key)
            )
            for key in ("SELECTED", "REVIEW", "FILTERED", "DUPLICATE")
        }
        report = {
            "mode": "OFFLINE_FICTITIOUS_DATA",
            "scheduler": "existing APScheduler; bounded CLI",
            "virtual_clock": True,
            "database": database.as_posix(),
            "categories": 9,
            **state,
            "dispositions_including_requeries": counts,
            "discovered_unique_products": session.scalar(select(func.count()).select_from(Product)),
            "selected_products": session.scalar(
                select(func.count()).select_from(ShopeeDiscoveryProduct)
            ),
            "history_observations": session.scalar(
                select(func.count()).select_from(ProductPriceHistory)
            ),
            "candidates": session.scalar(
                select(func.count()).select_from(ShopeeCollectionCandidate)
            ),
            "prices": {"first_fictitious": "100.00", "requery_fictitious": "90.00"},
            "availability": "UNKNOWN",
            "price_access": "UNKNOWN",
            "prepared_outbox": session.scalar(
                select(func.count()).select_from(PublicationQueueItem)
            ),
            "reason": "Unverified public price/stock and no own verified affiliate link",
            "real_shopee_calls": 0,
            "real_telegram_calls": 0,
            "real_affiliate_mutations": 0,
        }
    engine.dispose()
    if state["errors"]:
        raise RuntimeError("Offline demonstration failed: " + json.dumps(state["errors"]))
    return report


if __name__ == "__main__":
    evidence = run()
    output = Path("docs/DEMONSTRACAO_FASE_4_10.json")
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))

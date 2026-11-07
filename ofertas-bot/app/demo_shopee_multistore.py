"""Runnable single-page mock discovery and evidence review; never reads .env.

python -m app.demo_shopee_multistore [--output path/to/new-result.json]
The fixture, including its documented attributes, is entirely fictitious.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.http import JsonTransport
from app.integrations.shopee import ShopeeConnector
from app.models import Base
from app.services.shopee_discovery import ShopeeDiscoveryService
from app.services.shopee_multistore import ProductTarget, ShopeeMultistoreService

CATALOG = Path(__file__).resolve().parents[1] / "examples" / "multistore_defined_product.json"


def load_catalog(path=CATALOG):
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    if catalog["data_kind"] != "SYNTHETIC_ONLY":
        raise ValueError("A demonstração exige catálogo fictício explícito.")
    return catalog


def fixture_nodes(catalog):
    return [
        dict(
            shopId=row["shop_id"],
            itemId=row["item_id"],
            productName=row["name"],
            productLink=f"https://shopee.com.br/product/{row['shop_id']}/{row['item_id']}",
            price=row["price"],
            priceMin=row.get("price_min", row["price"]),
            priceMax=row.get("price_max", row["price"]),
            shopName=f"FICTÍCIA Loja {row['shop_id']}",
            ratingStar=row.get("rating_star", "4.8"),
            sales=0,
            shopType=[],
        )
        for row in catalog["listings"]
    ]


def fixture_evidence(catalog, page):
    """Bind explicitly fictitious documents to mock quotes; never infer facts from titles."""
    documents = {f"{row['shop_id']}:{row['item_id']}": row for row in catalog["listings"]}
    result = {}
    for snapshot in page.items:
        overrides = documents[snapshot.external_item_id]["evidence_overrides"]
        if overrides is not None:
            result[snapshot.external_item_id] = dict(
                facts={**catalog["documented_facts"], **overrides},
                source="MANUAL_DOCUMENTED",
                quote_fingerprint=snapshot.raw_fingerprint,
                reference=catalog.get(
                    "evidence_reference", "FICTÍCIO: examples/multistore_defined_product.json"
                ),
                actor="synthetic_fixture",
                verified_at=snapshot.observed_at,
            )
    return result


def run(catalog_path=CATALOG):
    catalog = load_catalog(catalog_path)
    product = ProductTarget.model_validate(catalog["product"])
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    calls = []

    def mock(request):
        calls.append(True)
        assert request.method == "POST" and len(calls) == 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "productOfferV2": {
                        "nodes": fixture_nodes(catalog),
                        "pageInfo": {"page": 1, "hasNextPage": True},
                    }
                }
            },
        )

    def deny(*args, **kwargs):
        raise AssertionError("HTTP externo proibido na demonstração.")

    with TemporaryDirectory(prefix="multistore-demo-") as directory:
        settings = Settings(
            _env_file=None,
            app_env="test",
            scheduler_enabled=False,
            database_url="sqlite:///" + (Path(directory) / "demo.simulation.db").as_posix(),
            shopee_app_id=SecretStr("SYNTHETIC_DEMO_APP"),
            shopee_secret=SecretStr("synthetic-demo-secret"),
        )
        engine = build_engine(settings.database_url)
        Base.metadata.create_all(engine)
        service = ShopeeDiscoveryService(build_session_factory(engine), settings, clock=lambda: now)
        with patch.object(httpx.HTTPTransport, "handle_request", deny):
            with httpx.Client(transport=httpx.MockTransport(mock), trust_env=False) as client:
                transport = JsonTransport(client=client, clock=lambda: now, min_interval=0)
                connector = ShopeeConnector(
                    transport,
                    enabled=True,
                    app_id=settings.shopee_app_id,
                    secret=settings.shopee_secret,
                    clock=lambda: now,
                )
                try:
                    page = ShopeeMultistoreService(service.collection).discover_offline(
                        product.search_query(), connector, limit=len(catalog["listings"])
                    )
                    result = service.review_defined_product(
                        page, product, evidence=fixture_evidence(catalog, page)
                    )
                    result.update(
                        data_kind="SYNTHETIC_ONLY",
                        discovery_mock_http_calls=len(calls),
                        returned_listings=len(page.items),
                    )
                    return service._json(result)
                finally:
                    engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--catalog", choices=("orion", "creatine"), default="orion")
    args = parser.parse_args()
    result = run(
        CATALOG if args.catalog == "orion" else CATALOG.with_name("multistore_creatine.json")
    )
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write("\n")
    summary = {
        key: result[key]
        for key in (
            "data_kind",
            "requested_product",
            "status",
            "returned_listings",
            "discovery_mock_http_calls",
            "external_http_calls",
            "history_writes",
        )
    }
    summary["proven_incompatible_count"] = len(result["proven_incompatible_products"])
    summary["filtered_count"] = len(result["filtered_candidates"])
    summary["inconclusive_count"] = len(result["unproven_candidates"])
    summary["groups"] = [
        dict(
            status=group["status"],
            distinct_shops=group["distinct_shops"],
            spread_amount=group["spread_amount"],
            ranking=[
                {
                    key: row[key]
                    for key in (
                        "rank",
                        "identity",
                        "shop_id",
                        "announced_price",
                        "difference_from_lowest",
                        "difference_from_lowest_percent",
                    )
                }
                for row in group["ranking"]
            ],
        )
        for group in result["proven_comparisons"]
    ]
    summary["unproven_candidate_count"] = len(result["unproven_candidates"])
    summary["lowest_price_highlights"] = result["lowest_price_highlights"]
    summary["opportunity_counts"] = result["opportunity_view"]["counts"]
    summary["commercial_handoff_allowed"] = result["opportunity_view"]["commercial_handoff"][
        "can_create_offer"
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

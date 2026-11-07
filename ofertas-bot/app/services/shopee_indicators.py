"""Partial product indicator semantics from the preserved official Explorer."""

import json
from functools import lru_cache
from pathlib import Path

from app.integrations.contracts import fingerprint

CONTRACT = Path(__file__).resolve().parents[2] / "docs/CONTRATO_INDICADORES_SHOPEE_20261021.json"


@lru_cache(maxsize=1)
def indicator_contract():
    value = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return value, fingerprint(value)


def product_indicators(row):
    contract, contract_hash = indicator_contract()
    seller = row.get("seller") or {}
    values = dict(
        ratingStar=seller.get("rating_star"),
        sales=seller.get("sales"),
        shopType=seller.get("shop_types"),
    )
    return dict(
        identity=row["identity"],
        observed_at=str(row["observed_at"]),
        source=row.get("source") or "PRESERVED_SHOPEE_RECORDING",
        source_operation="productOfferV2"
        if row.get("source") in (None, "shopee:official_api")
        else None,
        observation_fingerprint=row.get("quote_fingerprint") or row.get("observation_fingerprint"),
        fingerprint=fingerprint(
            dict(identity=row["identity"], observed_at=str(row["observed_at"]), fields=values)
        ),
        fields=values,
        field_presence={key: value is not None for key, value in values.items()},
        semantics=contract["ranking_semantics"],
        semantics_reference=contract["source_url"],
        semantics_sha256=contract_hash,
        source_html_sha256=contract["source_html_sha256"],
    )


def automatic_product_signals(row):
    """No rating scale/count, sales period, trend or seller reputation inferred."""
    indicators = product_indicators(row)
    if indicators["source_operation"] != "productOfferV2":
        return {}
    base = dict(
        subject_identity=row["identity"],
        source="OFFICIAL_API",
        source_url=indicators["semantics_reference"],
        actor="shopee_connector",
        reference="productOfferV2:" + row["identity"],
        source_sha256=indicators["fingerprint"],
        series_key="productOfferV2:" + row["identity"],
        semantics_reference="local-official-schema:" + indicators["semantics_sha256"],
        confirmed_semantics=True,
        observed_at=str(row["observed_at"]),
    )
    signals = {}
    if indicators["fields"]["ratingStar"] is not None:
        signals["reviews"] = dict(
            base, metric="PRODUCT_RATING_STAR_UNSCALED", value=indicators["fields"]["ratingStar"]
        )
    if indicators["fields"]["sales"] is not None:
        signals["popularity"] = dict(
            base,
            metric="PRODUCT_SALES_UNSPECIFIED_PERIOD",
            value=str(indicators["fields"]["sales"]),
        )
    return signals

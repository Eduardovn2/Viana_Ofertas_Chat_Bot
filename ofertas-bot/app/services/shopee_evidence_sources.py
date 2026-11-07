"""Capabilities supported by the local official contract, without entitlement assumptions."""

import json
from pathlib import Path

from app.integrations.contracts import fingerprint
from app.services.shopee_indicators import indicator_contract

CONTRACT = Path(__file__).resolve().parents[2] / "docs/CONTRATO_FONTES_SHOPEE_20261026.json"


def evidence_sources():
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    indicators, indicators_hash = indicator_contract()
    operations = {operation["name"] for operation in contract["operations"]}
    return dict(
        contract_review_date=contract["review_date"],
        contract_sha256=fingerprint(contract),
        indicator_contract_sha256=indicators_hash,
        cached_official_descriptions_source=indicators["source_url"],
        live_schema_checked=False,
        account_permissions_checked=False,
        external_http_calls=0,
        sources=[
            dict(
                source="productOfferV2",
                documented="productOfferV2" in operations,
                implemented=True,
                supports="Listing identity, title, announced quote and selected seller fields",
                structured_equivalence_attributes=False,
                ranking_field_semantics_confirmed=False,
                partially_confirmed={
                    "ratingStar": "PRODUCT_RATING_STAR",
                    "sales": "PRODUCT_SALES_COUNT",
                },
                ranking_fields_requiring_documentation=[
                    "rating_scale",
                    "review_count",
                    "sales_period",
                    "seller_reputation",
                ],
            ),
            dict(
                source="shopOfferV2",
                documented=True,
                account_permission="UNKNOWN",
                live_connector_implemented=False,
                offline_column_mapped_import=True,
                rating_subject_documented=False,
                availability_field_documented=False,
                own_affiliate_link_ownership_proven=False,
            ),
            dict(
                source="listItemFeeds/getItemFeedData",
                documented={"listItemFeeds", "getItemFeedData"} <= operations,
                implemented=False,
                account_permission="UNKNOWN",
                column_dictionary="UNKNOWN",
                supports="Feed rows expose columns as JSON; actual field names and meanings are not documented locally",
                automatic_requests_authorized=False,
                offline_column_mapped_import=True,
            ),
            dict(
                source="AFFILIATE_PORTAL_EXPORT",
                availability="USER_SUPPLIED_ONLY",
                implemented=True,
                preview_endpoint="/api/v1/manual-offers/official-exports/preview",
                import_endpoint="/api/v1/manual-offers/official-exports/import",
                requires_explicit_new_document_and_commercial_import_confirmation=True,
                requires_documented_column_semantics=True,
                current_commercial_data_requires_actual_observed_at=True,
                automatically_fetches_exports=False,
                ranking_preview_endpoint="/api/v1/shopee-discovery/ranking-signals/preview",
                ranking_import_endpoint="/api/v1/shopee-discovery/ranking-signals/import",
                ranking_requires_product_registration=False,
                supports="Documented columns actually present in an accessible official account export; never assumed",
            ),
            dict(
                source="OFFICIAL_MANUFACTURER_DOCUMENT",
                availability="USER_SUPPLIED_ONLY",
                supports="Exact product specifications; requires binding to the selected listing/variant",
                cannot_prove=[
                    "seller_condition",
                    "sale_quantity",
                    "price_conditions",
                    "stock",
                    "shipping",
                    "final_price",
                ],
            ),
            dict(
                source="MANUAL_LISTING_OBSERVATION",
                availability="USER_SUPPLIED_ONLY",
                supports="Documented observation of legally accessible listing and selected option; no scraping",
            ),
        ],
        unavailable_or_unconfirmed=[
            "seller_api_access",
            "stock",
            "selected_variant_api",
            "shipping",
            "confirmed_final_price",
            "coupon_applicability",
            "feed_column_semantics",
        ],
    )

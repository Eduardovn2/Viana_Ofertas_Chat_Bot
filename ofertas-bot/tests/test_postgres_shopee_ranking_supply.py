import pytest
import test_postgres_auto_pilot_real as pg
import test_shopee_ranking_supply as cases

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "case",
    [
        "import_previews_without_product_registration_and_never_creates_offers",
        "import_is_idempotent_and_source_values_feed_points",
        "contradictory_same_time_documents_do_not_choose_by_arrival",
        "documented_lifetime_series_can_feed_trend_without_assuming_api_sales_period",
        "stale_import_does_not_hide_current_partial_api_indicators",
        "worker_consumes_new_document_after_budget_without_new_search_or_offer",
        "mapped_feed_capture_requires_origin_authorization_but_does_not_call_feed",
        "shop_document_without_item_enriches_same_store_without_inventing_product_ids",
    ],
)
def test_postgres_ranking_supply(context, case):
    getattr(cases, "test_" + case)(context)

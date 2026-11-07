import pytest
import test_auto_pilot_window as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "name",
    [
        "new_plans_default_to_windowed_without_rewriting_legacy_scopes",
        "two_hours_distribute_twelve_queries_and_finish_at_deadline",
        "budget_exhaustion_keeps_processing_and_worker_waits_to_end",
        "ten_minute_three_query_pilot_does_not_end_in_ninety_seconds",
        "explicit_no_work_policy_can_end_early",
        "restart_keeps_budget_deadline_and_next_query_time",
        "late_tick_does_not_burst_to_catch_up",
        "pause_resume_preserves_schedule_anchor",
        "multiple_eligible_offers_publish_throughout_two_hours_without_coupons",
        "confirmed_official_export_can_supply_after_queries_are_exhausted",
        "uncertain_query_is_never_retried_after_restart",
        "query_spacing_is_signed_and_checked_on_direct_reservation",
        "opportunity_ranking_uses_only_proven_mean_and_median",
    ],
)
def test_postgres_window(context, name):
    getattr(cases, "test_" + name)(context)

import pytest
import test_auto_pilot_duration as cases
import test_postgres_auto_pilot_real as pg

categories_postgres = pg.categories_postgres
context = pg.context
no_network = pg.no_network
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "name",
    [
        "ten_minutes_start_at_activation_and_survive_restart",
        "pause_resume_never_extends_duration",
        "late_start_cannot_shorten_or_extend_signed_duration",
        "runtime_deadline_tampering_is_rejected",
        "worker_wait_respects_activation_deadline",
        "legacy_scope_does_not_acquire_new_fields",
    ],
)
def test_postgres_duration(context, name):
    getattr(cases, "test_" + name)(context)

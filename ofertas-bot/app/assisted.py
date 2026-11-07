"""Administrative preparation using existing routes; no background work or external dispatch.

uvicorn app.assisted:create_assisted_app --factory --host 127.0.0.1 --port 8000
"""

from app.core.config import Settings
from app.main import create_app


def create_assisted_app(settings: Settings | None = None):
    settings = settings or Settings()
    safe = settings.model_copy(
        update={
            "scheduler_enabled": False,
            "simulation_automation_enabled": False,
            "shopee_enabled": False,
            "shopee_discovery_enabled": False,
            "shopee_short_links_enabled": False,
            "mercado_livre_enabled": False,
            "mercadolivre_oauth_enabled": False,
            "telegram_publisher_enabled": False,
            "telegram_real_sends_authorized": False,
        }
    )
    app = create_app(safe, background_monitoring_allowed=False)
    app.state.execution_profile = "ASSISTED_PREPARATION"
    return app

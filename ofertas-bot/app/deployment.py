"""Administrative control API; no legacy scheduler or implicit external execution."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.assisted import create_assisted_app
from app.core.config import Settings
from app.models import AppSetting


def create_control_app(settings: Settings | None = None):
    app = create_assisted_app(settings)
    app.state.execution_profile = "AUTO_PILOT_CONTROL"
    from app.api.daily_operation import router as daily_router

    app.include_router(daily_router)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    expected_revision = ScriptDirectory.from_config(config).get_current_head()

    @app.get("/ready", tags=["Sistema"])
    def ready():
        try:
            with app.state.session_factory() as session:
                revision = session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                monitoring = session.scalar(
                    select(AppSetting.value).where(AppSetting.key == "monitoring_config")
                )
            valid = (
                revision == expected_revision
                and monitoring is not None
                and monitoring.get("scheduler_enabled") is False
                and app.state.settings.admin_api_key is not None
            )
        except SQLAlchemyError:
            valid = False
        return JSONResponse(
            status_code=200 if valid else 503,
            content={
                "status": "ready" if valid else "not_ready",
                "legacy_scheduler_started": False,
                "checks": "database_schema_persisted_pause_and_admin_configuration",
            },
        )

    return app

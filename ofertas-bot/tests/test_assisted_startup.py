"""The administrative backend must not activate background work or external services."""

import httpx
import pytest
import test_api as api
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.assisted import create_assisted_app
from app.jobs.runtime import MonitoringRuntime
from app.models import AppSetting
from app.services.configuration import save_monitoring_configuration

application = api.application


@pytest.mark.parametrize("persisted_enabled", [False, True])
def test_assisted_startup_never_starts_monitoring_even_if_persisted_enabled(
    application,
    monkeypatch,
    persisted_enabled,
):
    def forbidden(*args, **kwargs):
        pytest.fail("Preparation must not start scheduler or real HTTP")

    monkeypatch.setattr(MonitoringRuntime, "start", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    settings = application.state.settings.model_copy(
        update={
            "scheduler_enabled": True,
            "telegram_publisher_enabled": True,
            "telegram_real_sends_authorized": True,
            "shopee_enabled": True,
        }
    )
    app = create_assisted_app(settings)
    with app.state.session_factory() as session:
        save_monitoring_configuration(session, settings, {"scheduler_enabled": persisted_enabled})
        session.commit()
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/monitoring/status").status_code == 401
        status = client.get("/api/v1/monitoring/status", headers=api.HEADERS).json()
        assert status["running"] is False and status["background_monitoring_allowed"] is False
        for path, body in (
            ("/api/v1/monitoring/resume", {}),
            ("/api/v1/monitoring/config", {"scheduler_enabled": True}),
        ):
            response = (
                client.post(path, json=body, headers=api.HEADERS)
                if path.endswith("resume")
                else client.patch(path, json=body, headers=api.HEADERS)
            )
            assert response.status_code == 409
        assert app.state.monitoring._scheduler is None
        with app.state.session_factory() as session:
            assert (
                session.scalar(
                    select(AppSetting.value).where(AppSetting.key == "monitoring_config")
                )["scheduler_enabled"]
                == persisted_enabled
            )
        assert not app.state.settings.telegram_publisher_enabled
        assert not app.state.settings.telegram_real_sends_authorized
        assert not app.state.settings.shopee_enabled
        assert not app.state.settings.simulation_automation_enabled
    assert settings.telegram_publisher_enabled  # Caller settings were not mutated.

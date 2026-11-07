"""Local Windows service loop. Scheduling and authorization are independent of FastAPI."""

import logging
import signal
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db import build_engine, build_session_factory
from app.models import AppSetting
from app.services.affiliate_pipeline import REAL_LINK_PREFIX
from app.services.auto_pilot import PREFIX, TERMINAL
from app.services.auto_pilot_real import RealAutoPilotService
from app.services.auto_pilot_worker import AutoPilotWorker
from app.services.daily_operation import CONTROL, DAY, ZONE, ensure_setting, grant, setting
from app.services.leases import acquire_lease, release_lease

LEASE = "daily-operation-worker"
HEALTH = "daily_operation_health"
logger = logging.getLogger("ofertas_bot")


class DailyWorker:
    def __init__(self, controller):
        self.controller = controller
        self.factory = controller.factory
        self.settings = controller.settings
        self.poll_seconds = 30

    def _health(self, state, *, error=None, pilot_id=None):
        with self.factory() as session:
            row = ensure_setting(session, HEALTH, {})
            row.value = dict(
                state=state, at=self.controller._now().isoformat(), error=error, pilot_id=pilot_id
            )
            session.commit()
        return state

    def step(self):
        self.poll_seconds = 30
        owner = acquire_lease(self.factory, LEASE, 180, self.controller.clock)
        if owner is None:
            return "BUSY"
        try:
            return self._step()
        except Exception as error:
            self._health("ERROR", error=type(error).__name__)
            logger.error("daily_worker_error", extra={"error_type": type(error).__name__})
            return "ERROR"
        finally:
            release_lease(self.factory, LEASE, owner)

    def _close(self, key, pilot_id, reason):
        with self.controller._execution(pilot_id) as acquired:
            if acquired:
                self._close_exclusive(key, pilot_id, reason)

    def _close_exclusive(self, key, pilot_id, reason):
        with self.factory() as session:
            row = setting(session, PREFIX + pilot_id)
            value = row.value if row else None
        if value:
            report = self.controller.reconcile_interrupted(pilot_id, value["scope_sha256"])
            if report["state"] not in TERMINAL:
                self.controller.stop(pilot_id)
            report = self.controller.report(pilot_id)
        with self.factory() as session:
            row = setting(session, key, lock=True)
            summary = report["counts"] if value else {}
            links = []
            for item in report.get("daily_links", []) if value else []:
                batch = setting(session, REAL_LINK_PREFIX + item["id"], lock=True)
                if batch and batch.value["state"] == "CLAIMED":
                    batch.value = dict(
                        batch.value, state="UNCERTAIN", error_code="DAILY_WINDOW_CLOSED"
                    )
                links.append(dict(item, state=batch.value["state"] if batch else "ABANDONED"))
            row.value = dict(
                row.value,
                closed_at=self.controller._now().isoformat(),
                reason=reason,
                summary=summary,
                links=links,
            )
            session.commit()

    def _step(self):
        self.controller._safe()
        now = self.controller._now()
        today = now.astimezone(ZONE).date()
        key = DAY + today.isoformat()
        with self.factory() as session:
            rows = [
                (r.key, r.value)
                for r in session.scalars(select(AppSetting).where(AppSetting.key.startswith(DAY)))
                if not r.value.get("closed_at")
            ]
            stored = setting(session, CONTROL, lock=True)
            control = dict(stored.value) if stored else {"state": "STOPPED"}
            stopped_on = control.get("stopped_on")
            if (
                control["state"] == "STOPPED"
                and control.get("grant_id")
                and stopped_on
                and stopped_on < today.isoformat()
            ):
                _, policy = grant(session, self.settings, control["grant_id"])
                if policy.start_date <= today <= policy.end_date:
                    control["state"] = "RUNNING"
                    stored.value = control
                    session.commit()
        for run_key, run in rows:
            if (
                now >= datetime.fromisoformat(run["expires_at"])
                or control["state"] == "STOPPED"
                or run["grant_id"] != control.get("grant_id")
            ):
                self._close(
                    run_key,
                    run["pilot_id"],
                    "DEADLINE"
                    if now >= datetime.fromisoformat(run["expires_at"])
                    else "ADMIN_STOP",
                )
        if control["state"] == "STOPPED" or not control.get("grant_id"):
            return self._health("STOPPED")
        with self.factory() as session:
            _, plan = grant(session, self.settings, control["grant_id"])
            start, end = plan.window(today)
            if (
                not plan.start_date <= today <= plan.end_date
                or today.weekday() not in plan.days
                or not start <= now < end
            ):
                return self._health("PAUSED" if control["state"] == "PAUSED" else "WAITING")
            child = plan.child(today)
            pilot_id = child["authorization_id"]
            day = setting(session, key)
            if control["state"] == "RUNNING":
                day = ensure_setting(
                    session,
                    key,
                    dict(
                        grant_id=str(plan.authorization_id),
                        pilot_id=pilot_id,
                        expires_at=end.isoformat(),
                    ),
                )
                session.commit()
            if not day or day.value["pilot_id"] != pilot_id or day.value.get("closed_at"):
                return self._health("PAUSED" if control["state"] == "PAUSED" else "WAITING")
            pilot = setting(session, PREFIX + pilot_id)
            state = pilot.value["state"] if pilot else None
        if control["state"] == "PAUSED":
            if state == self.controller.active_state:
                self.controller.pause(pilot_id)
            return self._health("PAUSED", pilot_id=pilot_id)
        report = self.controller.report(pilot_id) if state else self.controller.prepare(child)
        if report["state"] in TERMINAL:
            self._close(key, pilot_id, report["reason"])
            return self._health("WAITING", pilot_id=pilot_id)
        digest = report["scope_sha256"]
        if report["state"] == "PREPARED":
            report = self.controller.approve(
                pilot_id,
                dict(
                    scope_sha256=digest,
                    actor=plan.actor,
                    confirmed_pilot_scope=True,
                    acknowledge_unconfirmed_quotas_and_permissions=True,
                ),
            )
        if report["state"] in {"APPROVED", "PAUSED"}:
            self.controller.start_authorized(pilot_id, digest)
        report = AutoPilotWorker(self.controller, pilot_id, digest).step()
        if (
            plan.operation_version == "DAILY_V3"
            and report["state"] == self.controller.active_state
            and len(report["reservations"]) < report["scope"]["plan"]["max_messages"]
            and any(item["state"] == "QUEUED" for item in report["queue"])
        ):
            report = self.controller.dispatch_batch(pilot_id)
            self.poll_seconds = 1
        if report["state"] in TERMINAL:
            self._close(key, pilot_id, report["reason"])
            return self._health("WAITING", pilot_id=pilot_id)
        return self._health("RUNNING", pilot_id=pilot_id)


def main():
    settings = Settings()
    configure_logging(settings.log_level)
    log_path = Path(".runtime/daily-worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    engine = build_engine(settings.database_url)
    controller = RealAutoPilotService(build_session_factory(engine), settings)
    event = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: event.set())
    try:
        worker = DailyWorker(controller)
        while not event.is_set():
            try:
                worker.step()
            except SQLAlchemyError as error:
                logger.error(
                    "daily_database_unavailable", extra={"error_type": type(error).__name__}
                )
            event.wait(worker.poll_seconds)
    finally:
        controller.close()
        engine.dispose()
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    main()

"""Deterministic process runtime; waits for signed administrative activation."""

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from sqlalchemy import select, text

from app.models import AppSetting
from app.services.auto_pilot import TERMINAL
from app.services.leases import conflict_insert

HEARTBEAT_PREFIX = "auto_pilot_worker:"
MAX_HEARTBEAT_AGE = 180
logger = logging.getLogger("ofertas_bot")


def worker_status(factory, pilot_id, now=None):
    now = now or datetime.now(UTC)
    with factory() as session:
        value = session.scalar(
            select(AppSetting.value).where(AppSetting.key == HEARTBEAT_PREFIX + str(pilot_id))
        )
    if not value:
        return dict(healthy=False, phase="NOT_STARTED")
    age = (now - datetime.fromisoformat(value["at"])).total_seconds()
    return dict(
        value,
        heartbeat_age_seconds=age,
        healthy=0 <= age <= MAX_HEARTBEAT_AGE and value["phase"] not in {"ERROR", "EXITED"},
    )


class AutoPilotWorker:
    def __init__(self, controller, pilot_id, scope_sha256, *, health_file=None, tick_seconds=30):
        self.controller = controller
        self.pilot_id = str(UUID(str(pilot_id)))
        self.scope_sha256 = scope_sha256
        self.worker_id = uuid4().hex
        self.health_file = Path(health_file) if health_file else None
        if tick_seconds != 30:
            raise ValueError("Worker limitado à cadência assinada de 30 segundos.")
        self.tick_seconds = tick_seconds

    def _heartbeat(self, phase, report=None, error_type=None):
        value = dict(
            at=self.controller._now().isoformat(),
            worker_id=self.worker_id,
            pilot_id=self.pilot_id,
            phase=phase,
            process_id=os.getpid(),
            state=report["state"] if report else None,
            error_type=error_type,
        )
        with self.controller.factory() as session:
            key = HEARTBEAT_PREFIX + self.pilot_id
            session.execute(
                conflict_insert(session, AppSetting)
                .values(key=key, value=value)
                .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
            )
            session.commit()
        if self.health_file:
            self.health_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.health_file.with_name(self.health_file.name + "." + self.worker_id)
            temporary.write_text(json.dumps(value), encoding="utf-8")
            os.replace(temporary, self.health_file)
        logger.info(
            "auto_pilot_worker_heartbeat",
            extra={k: value[k] for k in ("pilot_id", "worker_id", "phase", "state", "error_type")},
        )

    def step(self):
        report = self.controller.report(self.pilot_id)
        if (
            report["scope_sha256"] != self.scope_sha256
            or report["scope"].get("worker_tick_seconds") != self.tick_seconds
        ):
            raise ValueError("Worker não corresponde ao escopo assinado.")
        if report["counts"].get("unfinalized_reservations") or any(
            query["state"] == "RESERVED" for query in report["queries"]
        ):
            report = self.controller.reconcile_interrupted(self.pilot_id, self.scope_sha256)
        self._heartbeat(
            "TICK" if report["state"] == self.controller.active_state else "WAITING", report
        )
        if report["state"] == self.controller.active_state:
            report = self.controller.tick(self.pilot_id)
        self._heartbeat("TERMINAL" if report["state"] in TERMINAL else "WAITING", report)
        return report

    def run(self, stop_event=None, *, wait=None):
        event = stop_event or Event()
        waiter = wait or event.wait
        report = None
        try:
            while not event.is_set():
                tick_started = time.monotonic()
                report = self.step()
                if report["state"] in TERMINAL:
                    return report
                delay = max(0, self.tick_seconds - (time.monotonic() - tick_started))
                remaining = (
                    datetime.fromisoformat(
                        report.get("effective_expires_at", report["scope"]["plan"]["expires_at"])
                    )
                    - self.controller._now()
                ).total_seconds()
                if remaining > 0:
                    delay = min(delay, remaining)
                if delay:
                    waiter(delay)
            self._heartbeat("EXITED", report)
            return report
        except Exception as error:
            try:
                self._heartbeat("ERROR", report, type(error).__name__)
            except Exception:
                logger.error(
                    "auto_pilot_worker_health_unavailable",
                    extra={"error_type": type(error).__name__},
                )
            raise


def check_health(factory, path, pilot_id, *, now=None):
    """Local file + database; never contacts a provider or starts/resumes a pilot."""
    now = now or datetime.now(UTC)
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    age = (now - datetime.fromisoformat(value["at"])).total_seconds()
    if (
        value["pilot_id"] != str(pilot_id)
        or not 0 <= age <= MAX_HEARTBEAT_AGE
        or value["phase"] in {"ERROR", "EXITED"}
    ):
        return False
    with factory() as session:
        session.execute(text("SELECT 1"))
    return True

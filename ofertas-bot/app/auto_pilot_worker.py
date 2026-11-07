"""Independent deterministic worker. Never approves or starts a prepared pilot."""

import argparse
import logging
import signal
from threading import Event
from uuid import UUID

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db import build_engine, build_session_factory
from app.services.auto_pilot_real import RealAutoPilotService
from app.services.auto_pilot_worker import AutoPilotWorker, check_health


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-id", type=UUID)
    parser.add_argument("--scope-sha256")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    pilot_id = args.pilot_id or settings.auto_pilot_id
    scope_hash = args.scope_sha256 or settings.auto_pilot_scope_sha256
    if not pilot_id or not scope_hash:
        parser.error(
            "Configure AUTO_PILOT_ID/AUTO_PILOT_SCOPE_SHA256 ou os argumentos correspondentes."
        )
    configure_logging(settings.log_level)
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)
    service = None
    handlers = {}
    try:
        if args.healthcheck:
            raise SystemExit(
                0 if check_health(factory, settings.auto_pilot_health_file, pilot_id) else 1
            )
        event = Event()
        for number in (signal.SIGINT, signal.SIGTERM):
            handlers[number] = signal.signal(number, lambda *_: event.set())
        service = RealAutoPilotService(factory, settings)
        AutoPilotWorker(
            service,
            pilot_id,
            scope_hash,
            health_file=settings.auto_pilot_health_file,
            tick_seconds=settings.auto_pilot_worker_tick_seconds,
        ).run(event)
    except Exception as error:
        logging.getLogger("ofertas_bot").error(
            "auto_pilot_worker_failed", extra={"error_type": type(error).__name__}
        )
        raise SystemExit(1) from None
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
        if service:
            service.close()
        engine.dispose()


if __name__ == "__main__":
    main()

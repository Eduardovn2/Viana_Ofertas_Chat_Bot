"""Worker do modo Beta Shopee.

Comandos (PowerShell, na pasta ofertas-bot):
  .venv/Scripts/python.exe -m app.beta_worker run       # loop contínuo (Ctrl+C para sair)
  .venv/Scripts/python.exe -m app.beta_worker once      # um ciclo e sai
  .venv/Scripts/python.exe -m app.beta_worker once --dry-run   # força simulação
  .venv/Scripts/python.exe -m app.beta_worker report    # últimas ofertas e contagens
  .venv/Scripts/python.exe -m app.beta_worker pause     # pausa sem parar o processo
  .venv/Scripts/python.exe -m app.beta_worker resume
  .venv/Scripts/python.exe -m app.beta_worker health    # saída 0 se o worker está vivo
"""

import argparse
import json
import logging
import signal
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from threading import Event

from sqlalchemy import func, select

from app.beta.service import run_cycle
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db import build_engine, build_session_factory
from app.integrations.http import JsonTransport
from app.integrations.shopee import ShopeeConnector
from app.integrations.telegram import TelegramPublisher
from app.models.beta import BetaPost

logger = logging.getLogger("ofertas_bot.beta")


def build_connector(settings: Settings) -> ShopeeConnector:
    transport = JsonTransport(
        timeout=settings.integration_timeout_seconds,
        max_retries=settings.integration_max_retries,
        min_interval=settings.integration_min_interval_seconds,
        cache_seconds=settings.integration_cache_seconds,
        max_retry_wait=settings.integration_max_retry_wait_seconds,
    )
    return ShopeeConnector(
        transport,
        app_id=settings.shopee_app_id,
        secret=settings.shopee_secret,
        enabled=settings.shopee_enabled,
    )


def write_health(settings: Settings, payload: dict) -> None:
    path = settings.beta_health_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def check_health(settings: Settings, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    try:
        data = json.loads(settings.beta_health_file.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(data["heartbeat_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return now - last <= timedelta(minutes=settings.beta_interval_minutes * 2 + 5)


def cycle(settings, factory, connector, publisher, *, force_dry_run=False):
    now = datetime.now(UTC)
    with factory() as session:
        report = run_cycle(
            session,
            settings,
            connector,
            publisher,
            now=now,
            paused=settings.beta_pause_file.exists(),
            force_dry_run=force_dry_run,
        )
    data = asdict(report)
    logger.info("beta_cycle %s", json.dumps(data, ensure_ascii=False))
    write_health(settings, {"heartbeat_at": now.isoformat(), "last_cycle": data})
    return report


def print_report(settings, factory) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    with factory() as session:
        counts = dict(
            session.execute(
                select(BetaPost.status, func.count())
                .where(BetaPost.created_at >= since)
                .group_by(BetaPost.status)
            ).all()
        )
        rows = session.scalars(select(BetaPost).order_by(BetaPost.id.desc()).limit(20)).all()
    print(f"Beta ativo: {settings.beta_enabled} | dry-run: {settings.beta_dry_run}")
    print(f"Pausado: {settings.beta_pause_file.exists()} | worker vivo: {check_health(settings)}")
    print(f"Últimas 24h: {counts or 'nenhuma oferta'}")
    for row in rows:
        when = row.created_at.astimezone().strftime("%d/%m %H:%M")
        print(
            f"{when} {row.status:<9} {row.discount_percent:>2}% R$ {row.price_min:>9} "
            f"{row.title[:60]}"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Worker do modo Beta Shopee")
    parser.add_argument("command", choices=["run", "once", "report", "pause", "resume", "health"])
    parser.add_argument("--dry-run", action="store_true", help="Força simulação, sem Telegram")
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    if args.command == "pause":
        settings.beta_pause_file.parent.mkdir(parents=True, exist_ok=True)
        settings.beta_pause_file.write_text("paused\n", encoding="utf-8")
        print("Beta pausado. O worker para de publicar no próximo ciclo.")
        return 0
    if args.command == "resume":
        settings.beta_pause_file.unlink(missing_ok=True)
        print("Beta retomado.")
        return 0
    if args.command == "health":
        return 0 if check_health(settings) else 1
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)
    if args.command == "report":
        try:
            print_report(settings, factory)
        finally:
            engine.dispose()
        return 0
    connector = build_connector(settings)
    publisher = TelegramPublisher(settings)
    stop = Event()
    handlers = {}
    try:
        if args.command == "once":
            report = cycle(settings, factory, connector, publisher, force_dry_run=args.dry_run)
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
            return 0 if report.status != "ERROR" else 1
        for number in (signal.SIGINT, signal.SIGTERM):
            handlers[number] = signal.signal(number, lambda *_: stop.set())
        logger.info("beta_worker iniciado (intervalo %s min)", settings.beta_interval_minutes)
        while not stop.is_set():
            try:
                cycle(settings, factory, connector, publisher, force_dry_run=args.dry_run)
            except Exception as error:  # keep running; the next cycle may recover
                logger.error("beta_cycle_failed", extra={"error_type": type(error).__name__})
            stop.wait(settings.beta_interval_minutes * 60)
        logger.info("beta_worker encerrado")
        return 0
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
        publisher.close()
        connector.transport.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

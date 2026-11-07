"""Local preparation, login and health commands for the Windows application."""

import argparse
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app.api.daily_operation import LOGIN
from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.services.daily_operation import control, ensure_setting, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["database", "login", "prepare", "stop"])
    parser.add_argument("--scope", type=Path)
    args = parser.parse_args()
    settings = Settings()
    engine = build_engine(settings.database_url)
    try:
        with build_session_factory(engine)() as session:
            now = datetime.now(UTC)
            if args.action == "database":
                session.execute(text("SELECT 1"))
                print("PostgreSQL OK")
            elif args.action == "login":
                if settings.admin_api_key is None:
                    raise ValueError("Chave administrativa não configurada.")
                code = secrets.token_urlsafe(24)
                row = ensure_setting(session, LOGIN, {})
                row.value = dict(
                    digest=hashlib.sha256(code.encode()).hexdigest(),
                    expires_at=(now + timedelta(minutes=5)).isoformat(),
                    used=False,
                )
                session.commit()
                print(code)
            elif args.action == "prepare":
                if not args.scope:
                    parser.error("Informe --scope com o JSON do escopo.")
                result = prepare(
                    session, settings, json.loads(args.scope.read_text(encoding="utf-8"))
                )
                print(json.dumps(result, ensure_ascii=True, indent=2))
            else:
                print(json.dumps(control(session, settings, "stop", now)))
    except Exception as error:
        # Connection exceptions can contain sensitive connection information.
        raise SystemExit(type(error).__name__) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

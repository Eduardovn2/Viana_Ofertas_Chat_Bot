"""Explicit one-listing commercial read. Never prints credentials or raw API responses."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.db import build_engine, build_session_factory  # noqa: E402
from app.integrations.errors import IntegrationError  # noqa: E402
from app.services.commercial_validation import validate_listing  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", required=True)
    parser.add_argument("--affiliate-url", required=True)
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings()
        if settings.app_env == "test":
            raise ValueError
        engine = build_engine(settings.database_url)
        if engine.dialect.name != "postgresql":
            raise ValueError
        report = validate_listing(
            build_session_factory(engine), settings, args.item, args.affiliate_url
        )
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "SUCCEEDED" else 2
    except IntegrationError as error:
        print(json.dumps({"status": "FAILED", "error_code": error.code}))
        return 2
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

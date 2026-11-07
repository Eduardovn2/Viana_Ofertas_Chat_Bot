"""Prepare one future multistore authorization; never approve or dispatch HTTP.

python -m app.prepare_multistore_search --not-before 2026-11-02T12:00:00-03:00 \
    --expires-at 2026-11-02T12:10:00-03:00 --actor owner --output docs/search.json
"""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import MetaData, select, text

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.contracts import fingerprint
from app.services.shopee_discovery import ShopeeDiscoveryService
from app.services.shopee_multistore import ShopeeMultistoreAdapter


def snapshot(engine, excluded_key):
    """Hash existing rows read-only; exclude only this new preparation and audit."""
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        if connection.scalar(text("SELECT current_database()")) != "ofertas_bot":
            raise ValueError("Preparação exige banco operacional correto.")
        metadata = MetaData()
        metadata.reflect(connection, schema="public")
        result = {}
        for table in metadata.sorted_tables:
            statement = select(table)
            if table.name == "app_settings":
                statement = statement.where(table.c.key != excluded_key)
            elif table.name == "integration_events":
                statement = statement.where(
                    (table.c.source_reference != excluded_key) | table.c.source_reference.is_(None)
                )
            result[table.name] = sorted(
                fingerprint(dict(row)) for row in connection.execute(statement).mappings()
            )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--not-before", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--keyword", default="Cockpit simulador Brave Bull")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or not args.output.parent.is_dir():
        raise ValueError("Use um arquivo novo em diretório existente.")
    external_attempts = []

    def deny_http(*_args, **_kwargs):
        external_attempts.append(True)
        raise AssertionError("Preparação não pode realizar HTTP externo.")

    # Defense in depth: this command has no approve/execute branch.
    httpx.HTTPTransport.handle_request = deny_http
    httpx.AsyncHTTPTransport.handle_async_request = deny_http
    identity = str(uuid4())
    settings = Settings()
    engine = build_engine(settings.database_url)
    key = ShopeeMultistoreAdapter.key(identity)
    env_path = Path(".env")
    env_hash = hashlib.sha256(env_path.read_bytes()).hexdigest() if env_path.exists() else None
    try:
        before = snapshot(engine, key)
        service = ShopeeDiscoveryService(build_session_factory(engine), settings)
        plan = service.prepare_multistore_search(
            dict(
                authorization_id=identity,
                actor=args.actor,
                keyword=args.keyword,
                not_before=args.not_before,
                expires_at=args.expires_at,
            )
        )
        after = snapshot(engine, key)
        preserved = before == after
        env_preserved = env_hash == (
            hashlib.sha256(env_path.read_bytes()).hexdigest() if env_path.exists() else None
        )
        if not preserved or not env_preserved or external_attempts:
            raise AssertionError("Verificação de preservação falhou; não aprovar nem executar.")
        if (
            plan["status"] != "PREPARED"
            or plan["approval"] is not None
            or plan["requests_reserved"]
        ):
            raise AssertionError("Preparação não está no estado esperado.")
        plan.update(
            existing_operational_rows_preserved=preserved,
            env_preserved=env_preserved,
            external_posts=0,
            blocked_external_attempts=len(external_attempts),
            preparation_only=True,
        )
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(plan, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

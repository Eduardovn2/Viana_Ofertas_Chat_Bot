"""Execute one explicitly authorized, signed feed-metadata scope; never read rows."""

import argparse
import copy
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
from sqlalchemy import select, text

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.contracts import fingerprint
from app.integrations.http import JsonTransport
from app.integrations.shopee import ENDPOINT, ShopeeConnector
from app.models import AppSetting
from app.prepare_feed_probe import QUERY
from app.services.shopee_evidence import account_evidence, credential_binding
from app.services.simulation_safety import require_simulation_scope


def execute(
    factory,
    settings,
    identifier,
    digest,
    *,
    actor,
    confirmed,
    transport=None,
    isolated_testing=False,
    clock=None,
):
    if confirmed is not True:
        raise ValueError("Explicit authorization required")
    if isolated_testing:
        if type(transport) is not httpx.MockTransport:
            raise ValueError("Isolated execution requires MockTransport")
    elif transport is not None:
        raise ValueError("Production transport cannot be replaced")
    clock = clock or (lambda: datetime.now(UTC))
    key = "shopee_feed_probe:" + str(UUID(identifier))

    def load(session):
        if isolated_testing:
            require_simulation_scope(session, settings)
        elif session.get_bind().dialect.name != "postgresql" or session.execute(
            text("SELECT current_database(),current_schema()")
        ).one() != ("ofertas_bot", "public"):
            raise ValueError("Operational database required")
        row = session.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
        if row is None:
            raise ValueError("Feed authorization not found")
        value = copy.deepcopy(row.value)
        scope = value["scope"]
        signature = hmac.new(
            settings.shopee_secret.get_secret_value().encode(),
            ("feed-probe:" + fingerprint(scope)).encode(),
            hashlib.sha256,
        ).hexdigest()
        body = json.dumps({"query": QUERY}, ensure_ascii=False, separators=(",", ":")).encode()
        if (
            value["scope_sha256"] != digest
            or fingerprint(scope) != digest
            or not hmac.compare_digest(value["signature"], signature)
            or scope["authorization_id"] != str(UUID(identifier))
            or scope["operation"] != "listItemFeeds"
            or scope["mode"] != "REAL_FEED_METADATA_READ"
            or scope["endpoint"] != ENDPOINT
            or scope["payload"] != {"query": QUERY}
            or scope["payload_sha256"] != hashlib.sha256(body).hexdigest()
            or scope["credential_fingerprint"] != credential_binding(settings)
            or scope["max_http_attempts"] != 1
            or any(scope[k] is not False for k in ("retry", "fallback", "pagination"))
            or any(
                scope[k] != 0 for k in ("feed_row_reads", "link_generation", "telegram_messages")
            )
            or not datetime.fromisoformat(scope["not_before"])
            <= clock()
            < datetime.fromisoformat(scope["expires_at"])
            or account_evidence(
                session, settings, evidence_id=scope["account_evidence_id"], now=clock()
            )
            is None
        ):
            raise ValueError("Feed scope invalid or expired")
        return row, value, body

    with factory() as session:
        row, value, _ = load(session)
        if value["state"] != "PREPARED" or value["consumed"] or value["requests_reserved"]:
            raise ValueError("Feed scope already approved, reserved or consumed; no repeat")
        value.update(
            state="APPROVED",
            approval=dict(
                actor=actor,
                scope_sha256=digest,
                approved_at=clock().isoformat(),
                confirmed_single_metadata_read=True,
            ),
        )
        row.value = value
        session.commit()
    with factory() as session:
        row, value, _ = load(session)
        if value["state"] != "APPROVED" or value["consumed"] or value["requests_reserved"]:
            raise ValueError("Feed reservation unavailable")
        value.update(
            state="CLAIMED",
            consumed=True,
            requests_reserved=1,
            claimed_at=clock().isoformat(),
            http_started=False,
        )
        row.value = value
        session.commit()

    class Fence(httpx.BaseTransport):
        def __init__(self):
            self.delegate = transport if isolated_testing else httpx.HTTPTransport(retries=0)

        def handle_request(self, request):
            with factory() as session:
                row, value, body = load(session)
                if (
                    value["state"] != "CLAIMED"
                    or value.get("http_started")
                    or value["requests_reserved"] != 1
                    or not value["consumed"]
                    or request.method != "POST"
                    or str(request.url) != ENDPOINT
                    or request.content != body
                ):
                    raise ValueError("Feed HTTP permit unavailable")
                value.update(http_started=True, http_started_at=clock().isoformat())
                row.value = value
                session.commit()
            response = self.delegate.handle_request(request)
            with factory() as session:
                row = session.scalar(
                    select(AppSetting).where(AppSetting.key == key).with_for_update()
                )
                row.value = dict(row.value, http_status=response.status_code)
                session.commit()
            return response

        def close(self):
            self.delegate.close()

    outcome = {}
    try:
        with httpx.Client(
            transport=Fence(), timeout=10, follow_redirects=False, trust_env=False
        ) as client:
            connector = ShopeeConnector(
                JsonTransport(
                    client=client, max_retries=0, cache_seconds=0, min_interval=0, clock=clock
                ),
                settings.shopee_app_id,
                settings.shopee_secret,
                enabled=True,
                clock=clock,
            )
            response = connector._request(QUERY, cache=False, retry=False)
            data = response.data.get("data", {}).get("listItemFeeds")
            if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
                raise ValueError("INVALID_FEED_METADATA_RESPONSE")
            fields = {
                "datafeedId",
                "referenceId",
                "datafeedName",
                "description",
                "totalCount",
                "date",
                "feedMode",
            }
            if any(not isinstance(feed, dict) for feed in data["feeds"]):
                raise ValueError("INVALID_FEED_METADATA_RESPONSE")
            outcome = dict(
                state="SUCCEEDED",
                feeds=[{k: v for k, v in feed.items() if k in fields} for feed in data["feeds"]],
                feed_count=len(data["feeds"]),
                rows_read=0,
            )
    except Exception as exc:
        # Once reserved, any ambiguous failure remains consumed and is never retried.
        outcome = dict(
            state="UNCERTAIN", error_code=getattr(exc, "code", type(exc).__name__), rows_read=0
        )
    finally:
        with factory() as session:
            row = session.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
            row.value = dict(row.value, **outcome, finished_at=clock().isoformat())
            session.commit()
            result = {k: v for k, v in row.value.items() if k != "signature"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--scope-sha256", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--authorization-confirmed", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    engine = build_engine(settings.database_url)
    try:
        result = execute(
            build_session_factory(engine),
            settings,
            args.id,
            args.scope_sha256,
            actor=args.actor,
            confirmed=args.authorization_confirmed,
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

"""Prepare one feed-metadata read. No approval, execution or provider I/O."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.contracts import fingerprint
from app.integrations.shopee import ENDPOINT
from app.models import AppSetting
from app.services.shopee_evidence import account_evidence, credential_binding

QUERY = "query { listItemFeeds { feeds { datafeedId referenceId datafeedName description totalCount date feedMode } } }"


def prepare(session, settings, *, now=None):
    now = now or datetime.now(UTC)
    proof = account_evidence(session, settings, now=now)
    if proof is None:
        raise ValueError("Current documented affiliate account required")
    identifier = str(uuid4())
    payload = {"query": QUERY}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    scope = dict(
        authorization_id=identifier,
        operation="listItemFeeds",
        mode="REAL_FEED_METADATA_READ",
        endpoint=ENDPOINT,
        payload=payload,
        payload_sha256=hashlib.sha256(raw).hexdigest(),
        credential_fingerprint=credential_binding(settings),
        account_evidence_id=proof["id"],
        account_reference=proof["account_reference"],
        not_before=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        max_http_attempts=1,
        retry=False,
        fallback=False,
        pagination=False,
        feed_row_reads=0,
        link_generation=0,
        telegram_messages=0,
    )
    digest = fingerprint(scope)
    signature = hmac.new(
        settings.shopee_secret.get_secret_value().encode(),
        ("feed-probe:" + digest).encode(),
        hashlib.sha256,
    ).hexdigest()
    value = dict(
        state="PREPARED",
        scope=scope,
        scope_sha256=digest,
        signature=signature,
        consumed=False,
        requests_reserved=0,
    )
    session.add(AppSetting(key="shopee_feed_probe:" + identifier, value=value))
    session.flush()
    return {k: v for k, v in value.items() if k != "signature"}


def main():
    settings = Settings()
    engine = build_engine(settings.database_url)
    try:
        with build_session_factory(engine)() as session:
            result = prepare(session, settings)
            session.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

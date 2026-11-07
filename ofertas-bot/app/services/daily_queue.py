"""Persistent discovery backlog and provider pacing for the continuous policy."""

import math
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import func, select

from app.integrations.contracts import ProductRef, fingerprint
from app.models import AppSetting, Platform
from app.services.daily_operation import ZONE, ensure_setting, setting

QUEUE = "daily_candidate:"


def continuous(value):
    policy = value["scope"]["plan"].get("operating_policy") or {}
    return policy.get("telegram_per_minute") is not None


def queue_prefix(value):
    return QUEUE + value["scope"]["plan"]["daily_grant_id"] + ":"


def candidates(value):
    return select(AppSetting).where(AppSetting.key.startswith(queue_prefix(value)))


def record_capture(session, value, query, now):
    """Keep each identity independently of the daily pilot's outbox queue."""
    if not continuous(value):
        return
    quotes = query.get("capture", {}).get("comparison", {}).get("candidates", [])
    for quote in quotes:
        identity = quote["identity"]
        state = "REJECTED" if quote["status"] == "REJECTED" else "PENDING"
        row = ensure_setting(session, queue_prefix(value) + identity, {})
        old = row.value
        if old.get("state") in {"SENT", "UNCERTAIN"}:
            continue
        row.value = dict(
            old,
            identity=identity,
            name=quote["name"],
            term=query["term"],
            state=state,
            observed_at=datetime.fromisoformat(quote["observed_at"]).astimezone(UTC).isoformat(),
            reason=quote.get("reason"),
            capture_id=query["id"],
            reviewed_at=now.isoformat(),
        )
    if query.get("refresh_identity") and query["state"] == "SUCCEEDED" and not quotes:
        row = setting(session, queue_prefix(value) + query["refresh_identity"], lock=True)
        if row and row.value["state"] == "PENDING":
            row.value = dict(row.value, state="REJECTED", reason="PRODUCT_NOT_FOUND")


def refresh_candidate(session, value, now):
    """Alternate new searches with stale pending work, with no backlog size limit."""
    if not continuous(value) or (
        value["scope"]["plan"]["max_queries"] > 1 and len(value["queries"]) % 2 == 0
    ):
        return None
    if len(value["reservations"]) >= value["scope"]["plan"]["max_messages"]:
        return None
    today = now.astimezone(ZONE).date().isoformat()
    fields = AppSetting.value
    row = session.scalar(
        candidates(value)
        .where(
            fields["state"].as_string() == "PENDING",
            fields["observed_at"].as_string()
            <= (now.astimezone(UTC) - timedelta(seconds=600)).isoformat(),
            func.coalesce(fields["refresh_day"].as_string(), "") != today,
        )
        .order_by(fields["reviewed_at"].as_string(), AppSetting.id)
        .limit(1)
        .with_for_update()
    )
    if row is not None:
        row.value = dict(row.value, refresh_day=today, reviewed_at=now.isoformat())
    return row.value if row else None


def refresh_ref(identity):
    return ProductRef(
        Platform.SHOPEE, identity, "https://shopee.com.br/product/" + identity.replace(":", "/")
    )


def record_dispatch(session, value, identity, state, reason):
    if not continuous(value):
        return
    row = setting(session, queue_prefix(value) + identity, lock=True)
    if row and row.value["state"] not in {"SENT", "UNCERTAIN"}:
        final = state if state in {"SENT", "UNCERTAIN"} else "PENDING"
        row.value = dict(row.value, state=final, reason=reason)


def _rate_key(settings):
    return "daily_shopee_rate:" + fingerprint(settings.shopee_app_id.get_secret_value())


def shopee_ready(session, settings, limit, now, *, reserve=False):
    if limit is None:
        return True
    return provider_ready(session, _rate_key(settings), limit, now, reserve=reserve)


def telegram_ready(session, chat_id, limit, now, *, reserve=False):
    return provider_ready(
        session,
        "daily_telegram_rate:" + str(chat_id),
        limit,
        now,
        reserve=reserve,
        minimum_spacing=1,
    )


def provider_ready(session, key, limit, now, *, reserve=False, minimum_spacing=0):
    row = ensure_setting(session, key, {}) if reserve else setting(session, key)
    value = row.value if row else {}
    cutoff = now - timedelta(seconds=60)
    recent = [at for at in value.get("posts", []) if datetime.fromisoformat(at) > cutoff]
    until = value.get("deferred_until")
    if (
        len(recent) >= limit
        or (until and datetime.fromisoformat(until) > now)
        or (
            recent and now < datetime.fromisoformat(recent[-1]) + timedelta(seconds=minimum_spacing)
        )
    ):
        return False
    if reserve:
        row.value = dict(value, posts=[*recent, now.isoformat()])
    return True


def defer_shopee(factory, settings, response, now):
    """Honor HTTP Retry-After durably across restarts and both Shopee operations."""
    if response.status_code != 429:
        return
    raw = response.headers.get("Retry-After", "")
    try:
        delay = float(raw)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError
        until = now + timedelta(seconds=delay)
    except OverflowError:
        until = datetime.max.replace(tzinfo=UTC)
    except ValueError:
        try:
            until = max(now, parsedate_to_datetime(raw))
        except (ValueError, TypeError, OverflowError):
            until = now + timedelta(minutes=1)
    with factory() as session:
        row = ensure_setting(session, _rate_key(settings), {})
        previous = row.value.get("deferred_until")
        if not previous or until > datetime.fromisoformat(previous):
            row.value = dict(row.value, deferred_until=until.isoformat())
        session.commit()

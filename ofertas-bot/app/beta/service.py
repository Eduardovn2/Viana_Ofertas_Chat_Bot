"""One beta cycle: search, filter, deduplicate, recheck, publish (or simulate).

Safety rails, all configurable in .env:
- BETA_ENABLED=false (default) or the pause file → the cycle does nothing.
- BETA_DRY_RUN=true (default) → rows are recorded as DRY_RUN; Telegram is never called.
- Real sends still go through TelegramPublisher, which requires
  TELEGRAM_PUBLISHER_ENABLED, TELEGRAM_REAL_SENDS_AUTHORIZED and the chat in
  TELEGRAM_ALLOWED_CHAT_IDS.
- Hourly/daily limits, quiet hours and a repost cooldown per item.
- A row is committed as SENDING before the HTTP call, so a crash never causes a
  silent duplicate: an unfinished row counts as published.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.beta.offers import BetaOffer, OfferRejected, parse_offer, rejection_reason, render, score
from app.integrations.errors import IntegrationError
from app.integrations.telegram import TelegramPublishError
from app.models.beta import BetaPost
from app.services.message_templates import MessageTemplateError

logger = logging.getLogger("ofertas_bot.beta")
_PUBLISHED = ("SENT", "SENDING", "UNCERTAIN")
# Shopee answers "System Error" (code 10000) intermittently; retry before giving up.
SEARCH_RETRY_WAITS = (2, 5)


@dataclass
class CycleReport:
    status: str = "OK"
    mode: str = "DRY_RUN"
    keywords: list[str] = field(default_factory=list)
    found: int = 0
    eligible: int = 0
    retries: int = 0
    published: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def reject(self, reason: str):
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def local_zone(settings):
    """IANA zone; falls back to UTC-3 (Brasília, no DST) when tzdata is missing."""
    try:
        return ZoneInfo(settings.beta_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone(timedelta(hours=-3))


def in_quiet_hours(settings, now: datetime) -> bool:
    start, end = settings.beta_quiet_hours_start, settings.beta_quiet_hours_end
    if start == end:
        return False
    hour = now.astimezone(local_zone(settings)).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def keywords_for_cycle(settings, now: datetime) -> list[str]:
    """Rotate through BETA_KEYWORDS so each cycle searches a different slice."""
    keywords = list(settings.beta_keywords)
    if not keywords:
        return []
    count = min(settings.beta_keywords_per_cycle, len(keywords))
    slot = int(now.timestamp() // (settings.beta_interval_minutes * 60))
    start = (slot * count) % len(keywords)
    return [keywords[(start + i) % len(keywords)] for i in range(count)]


def _statuses(dry_run: bool):
    return ("DRY_RUN",) if dry_run else _PUBLISHED


def remaining_allowance(session: Session, settings, now: datetime, dry_run: bool) -> int:
    def count(since):
        return session.scalar(
            select(func.count())
            .select_from(BetaPost)
            .where(BetaPost.status.in_(_statuses(dry_run)), BetaPost.created_at >= since)
        )

    hour_left = settings.beta_max_posts_per_hour - count(now - timedelta(hours=1))
    day_left = settings.beta_max_posts_per_day - count(now - timedelta(days=1))
    return max(0, min(settings.beta_max_posts_per_cycle, hour_left, day_left))


def is_duplicate(
    session: Session, settings, offer: BetaOffer, now: datetime, dry_run: bool
) -> bool:
    """Same item inside the cooldown is a duplicate, unless its price dropped enough."""
    since = now - timedelta(days=settings.beta_repost_cooldown_days)
    last_price = session.scalar(
        select(BetaPost.price_min)
        .where(
            BetaPost.item_key == offer.item_key,
            BetaPost.status.in_(_statuses(dry_run)),
            BetaPost.created_at >= since,
        )
        .order_by(BetaPost.created_at.desc(), BetaPost.id.desc())
        .limit(1)
    )
    if last_price is None:
        return False
    threshold = Decimal(100 - settings.beta_repost_min_price_drop_percent) / 100
    return offer.price_min > last_price * threshold


def search_with_retry(connector, settings, keyword, report, sleep):
    waits = (*SEARCH_RETRY_WAITS, None)
    for attempt, wait in enumerate(waits, start=1):
        try:
            nodes, _ = connector.beta_search(
                keyword, limit=settings.beta_search_limit, sort_type=settings.beta_sort_type
            )
            return nodes
        except IntegrationError as error:
            if wait is None or error.code == "CONNECTOR_DISABLED":
                report.errors.append(f"SEARCH_{error.code}")
                logger.warning(
                    "beta: busca Shopee falhou (%s) para %r após %s tentativa(s)",
                    error.code,
                    keyword,
                    attempt,
                )
                return None
            report.retries += 1
            sleep(wait)
    return None


def collect_candidates(session, settings, connector, now, report, dry_run, sleep=time.sleep):
    secret = settings.shopee_secret.get_secret_value() if settings.shopee_secret else None
    best: dict[str, tuple[Decimal, BetaOffer, str]] = {}
    for keyword in report.keywords:
        nodes = search_with_retry(connector, settings, keyword, report, sleep)
        if nodes is None:
            continue
        report.found += len(nodes)
        for node in nodes:
            try:
                offer = parse_offer(node, secret=secret)
            except OfferRejected as error:
                report.reject(error.reason)
                continue
            reason = rejection_reason(offer, settings)
            if reason is None and is_duplicate(session, settings, offer, now, dry_run):
                reason = "DUPLICATE"
            if reason is not None:
                report.reject(reason)
                continue
            value = score(offer)
            if offer.item_key not in best or best[offer.item_key][0] < value:
                best[offer.item_key] = (value, offer, keyword)
    report.eligible = len(best)
    return sorted(best.values(), key=lambda row: (row[0], row[1].item_key), reverse=True)


def recheck(connector, settings, offer: BetaOffer, report) -> BetaOffer | None:
    """Fresh read right before sending; drop the offer if it got worse or vanished."""
    secret = settings.shopee_secret.get_secret_value() if settings.shopee_secret else None
    try:
        node, _ = connector.beta_lookup(offer.shop_id, offer.item_id)
    except IntegrationError as error:
        report.reject(f"RECHECK_{error.code}")
        return None
    if node is None:
        report.reject("RECHECK_NOT_FOUND")
        return None
    try:
        fresh = parse_offer(node, secret=secret)
    except OfferRejected as error:
        report.reject(f"RECHECK_{error.reason}")
        return None
    if fresh.item_key != offer.item_key:
        report.reject("RECHECK_MISMATCH")
        return None
    if fresh.price_min > offer.price_min:
        report.reject("RECHECK_PRICE_UP")
        return None
    reason = rejection_reason(fresh, settings)
    if reason is not None:
        report.reject(f"RECHECK_{reason}")
        return None
    return fresh


def _row(offer, keyword, value, rendered, status, chat_id, now):
    return BetaPost(
        item_key=offer.item_key,
        title=offer.title,
        keyword=keyword,
        price_min=offer.price_min,
        price_max=offer.price_max,
        discount_percent=offer.discount_percent,
        rating=offer.rating,
        sales=offer.sales,
        commission_rate=offer.commission_rate,
        score=value,
        offer_link=offer.offer_link,
        image_url=offer.image_url,
        text=rendered.text,
        status=status,
        chat_id=chat_id,
        created_at=now,
        updated_at=now,
    )


def run_cycle(
    session: Session,
    settings,
    connector,
    publisher,
    *,
    now: datetime,
    paused: bool = False,
    force_dry_run: bool = False,
    sleep=time.sleep,
) -> CycleReport:
    dry_run = bool(settings.beta_dry_run or force_dry_run)
    report = CycleReport(mode="DRY_RUN" if dry_run else "LIVE")
    if not settings.beta_enabled:
        report.status = "DISABLED"
        return report
    if paused:
        report.status = "PAUSED"
        return report
    if not dry_run and not settings.beta_chat_id:
        report.status = "MISSING_CHAT"
        return report
    if in_quiet_hours(settings, now):
        report.status = "QUIET_HOURS"
        return report
    allowance = remaining_allowance(session, settings, now, dry_run)
    if allowance == 0:
        report.status = "LIMIT_REACHED"
        return report
    report.keywords = keywords_for_cycle(settings, now)
    if not report.keywords:
        report.status = "NO_KEYWORDS"
        return report
    candidates = collect_candidates(session, settings, connector, now, report, dry_run, sleep)
    # Bound the fresh reads per cycle even when many candidates fail the recheck.
    for value, offer, keyword in candidates[: allowance * 3]:
        if report.published >= allowance:
            break
        fresh = recheck(connector, settings, offer, report)
        if fresh is None:
            continue
        try:
            rendered = render(fresh)
        except MessageTemplateError:
            report.reject("RENDER_FAILED")
            continue
        if dry_run:
            session.add(_row(fresh, keyword, value, rendered, "DRY_RUN", None, now))
            session.commit()
            report.published += 1
            logger.info(
                "beta[dry-run]: %s | %s | %s%%", fresh.item_key, fresh.title, fresh.discount_percent
            )
            continue
        row = _row(fresh, keyword, value, rendered, "SENDING", settings.beta_chat_id, now)
        session.add(row)
        session.commit()
        try:
            result = publisher.send(settings.beta_chat_id, rendered)
        except TelegramPublishError as error:
            row.status = "UNCERTAIN" if error.uncertain else "FAILED"
            row.error_code = error.code
            session.commit()
            report.errors.append(f"TELEGRAM_{error.code}")
            logger.warning("beta: envio Telegram falhou (%s) para %s", error.code, fresh.item_key)
            if error.code != "INVALID_MESSAGE":
                break  # configuration, permission or rate problems: stop this cycle
            continue
        row.status = "SENT"
        row.telegram_message_id = str(result.message_id)
        row.sent_at = now
        session.commit()
        report.published += 1
        logger.info("beta: publicado %s | %s", fresh.item_key, fresh.title)
    if report.errors and report.published == 0:
        report.status = "ERROR"
    return report

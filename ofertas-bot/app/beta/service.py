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

from app.beta.offers import (
    BetaOffer,
    OfferRejected,
    has_excluded_term,
    matches_keyword,
    parse_offer,
    rejection_reason,
    render,
    render_whatsapp,
    score,
)
from app.integrations.errors import IntegrationError
from app.integrations.telegram import TelegramPublishError
from app.integrations.whatsapp import WhatsAppPublishError
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
    searches: list[str] = field(default_factory=list)
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


def _rotation(settings, now: datetime) -> tuple[list[str], int]:
    """All keywords starting at this cycle's slot, and how many make up one slice."""
    keywords = list(settings.beta_keywords)
    if not keywords:
        return [], 0
    count = min(settings.beta_keywords_per_cycle, len(keywords))
    slot = int(now.timestamp() // (settings.beta_interval_minutes * 60))
    start = (slot * count) % len(keywords)
    return [keywords[(start + i) % len(keywords)] for i in range(len(keywords))], count


def keywords_for_cycle(settings, now: datetime) -> list[str]:
    """Rotate through BETA_KEYWORDS so each cycle searches a different slice."""
    ordered, count = _rotation(settings, now)
    return ordered[:count]


class SearchPlan:
    """Order of (keyword, page) searches for one cycle.

    First page 1 of this cycle's slice (the usual searches). If that is not
    enough, deeper pages of the same slice, then the next slices of the rotation.
    A keyword that failed or has no next page is skipped from then on.
    """

    def __init__(self, settings, now: datetime):
        ordered, count = _rotation(settings, now)
        self.groups = [ordered[i : i + count] for i in range(0, len(ordered), count or 1)]
        self.max_pages = settings.beta_max_pages_per_keyword
        self.done: set[str] = set()
        self._steps = self._iterate()

    def _iterate(self):
        for group in self.groups:
            for page in range(1, self.max_pages + 1):
                for keyword in group:
                    if keyword not in self.done:
                        yield keyword, page

    def next(self) -> tuple[str, int] | None:
        return next(self._steps, None)


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


def search_with_retry(connector, settings, keyword, report, sleep, page=1):
    """(nodes, has_next) or None when the search kept failing; never raises."""
    waits = (*SEARCH_RETRY_WAITS, None)
    for attempt, wait in enumerate(waits, start=1):
        try:
            return connector.beta_search(
                keyword,
                limit=settings.beta_search_limit,
                sort_type=settings.beta_sort_type,
                page=page,
            )
        except IntegrationError as error:
            if wait is None or error.code == "CONNECTOR_DISABLED":
                report.errors.append(f"SEARCH_{error.code}")
                upstream = getattr(error, "upstream_code", None)
                logger.warning(
                    "beta: busca Shopee falhou (%s%s) para %r página %s após %s tentativa(s);"
                    " seguindo para a próxima busca",
                    error.code,
                    f" código {upstream}" if upstream is not None else "",
                    keyword,
                    page,
                    attempt,
                )
                return None
            report.retries += 1
            sleep(wait)
    return None


def evaluate_nodes(session, settings, nodes, keyword, now, report, dry_run, best) -> None:
    """Filter one page of results into `best` (item_key -> (score, offer, keyword))."""
    secret = settings.shopee_secret.get_secret_value() if settings.shopee_secret else None
    report.found += len(nodes)
    for node in nodes:
        try:
            offer = parse_offer(node, secret=secret)
        except OfferRejected as error:
            report.reject(error.reason)
            continue
        reason = rejection_reason(offer, settings)
        if reason is None and has_excluded_term(offer.title, settings.beta_excluded_terms):
            reason = "EXCLUDED_TERM"
        if (
            reason is None
            and settings.beta_require_keyword_in_title
            and not matches_keyword(offer.title, keyword, settings.beta_broad_keywords)
        ):
            reason = "OFF_TOPIC"
        if reason is None and is_duplicate(session, settings, offer, now, dry_run):
            reason = "DUPLICATE"
        if reason is not None:
            report.reject(reason)
            continue
        value = score(offer)
        if offer.item_key not in best or best[offer.item_key][0] < value:
            best[offer.item_key] = (value, offer, keyword)


def run_search(session, settings, connector, plan, now, report, dry_run, best, sleep) -> bool:
    """Run the next planned search; False when the plan is exhausted."""
    step = plan.next()
    if step is None:
        return False
    keyword, page = step
    report.searches.append(keyword if page == 1 else f"{keyword} p{page}")
    result = search_with_retry(connector, settings, keyword, report, sleep, page=page)
    if result is None:
        plan.done.add(keyword)  # skip this term for the rest of the cycle
        return True
    nodes, has_next = result
    if not has_next or not nodes:
        plan.done.add(keyword)
    evaluate_nodes(session, settings, nodes, keyword, now, report, dry_run, best)
    return True


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


def active_channels(settings, whatsapp) -> list[str]:
    channels = []
    if "telegram" in settings.beta_channels and settings.beta_chat_id:
        channels.append("telegram")
    if "whatsapp" in settings.beta_channels and settings.whatsapp_chat_id and whatsapp:
        channels.append("whatsapp")
    return channels


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
    whatsapp=None,
) -> CycleReport:
    """`publisher` sends to Telegram; `whatsapp` (optional) to the WhatsApp gateway."""
    dry_run = bool(settings.beta_dry_run or force_dry_run)
    report = CycleReport(mode="DRY_RUN" if dry_run else "LIVE")
    if not settings.beta_enabled:
        report.status = "DISABLED"
        return report
    if paused:
        report.status = "PAUSED"
        return report
    channels = active_channels(settings, whatsapp)
    if not dry_run and not channels:
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
    plan = SearchPlan(settings, now)
    best: dict[str, tuple[Decimal, BetaOffer, str]] = {}
    tried: set[str] = set()
    rechecks = 0
    # Bound the fresh reads per cycle even when many candidates fail the recheck.
    max_rechecks = allowance * 3
    for value, offer, keyword in _candidates(
        session, settings, connector, plan, now, report, dry_run, best, tried, allowance, sleep
    ):
        if (
            report.published >= allowance
            or (not dry_run and not channels)
            or rechecks >= max_rechecks
        ):
            break
        rechecks += 1
        fresh = recheck(connector, settings, offer, report)
        if fresh is None:
            continue
        try:
            rendered = render(fresh, ad_label=settings.beta_ad_label)
            wa_rendered = render_whatsapp(fresh, ad_label=settings.beta_ad_label)
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
        chat_id = settings.beta_chat_id if "telegram" in channels else None
        row = _row(fresh, keyword, value, rendered, "SENDING", chat_id, now)
        if "whatsapp" in channels:
            row.whatsapp_status = "SENDING"
        session.add(row)
        session.commit()
        sent = uncertain = False
        for channel in list(channels):
            if channel == "telegram":
                try:
                    result = publisher.send(settings.beta_chat_id, rendered)
                except TelegramPublishError as error:
                    uncertain = uncertain or error.uncertain
                    row.error_code = row.error_code or error.code
                    report.errors.append(f"TELEGRAM_{error.code}")
                    logger.warning(
                        "beta: envio Telegram falhou (%s) para %s", error.code, fresh.item_key
                    )
                    if error.code != "INVALID_MESSAGE":
                        channels.remove("telegram")  # config/permission/rate: skip this cycle
                    continue
                row.telegram_message_id = str(result.message_id)
                sent = True
            else:
                try:
                    message_id = whatsapp.send(settings.whatsapp_chat_id, wa_rendered)
                except WhatsAppPublishError as error:
                    uncertain = uncertain or error.uncertain
                    row.whatsapp_status = "UNCERTAIN" if error.uncertain else "FAILED"
                    row.error_code = row.error_code or f"WHATSAPP_{error.code}"
                    report.errors.append(f"WHATSAPP_{error.code}")
                    logger.warning(
                        "beta: envio WhatsApp falhou (%s) para %s", error.code, fresh.item_key
                    )
                    if error.code != "INVALID_MESSAGE":
                        channels.remove("whatsapp")
                    continue
                row.whatsapp_status = "SENT"
                row.whatsapp_message_id = message_id
                sent = True
        if sent:
            row.status = "SENT"
            row.sent_at = now
            report.published += 1
            logger.info("beta: publicado %s | %s", fresh.item_key, fresh.title)
        else:
            row.status = "UNCERTAIN" if uncertain else "FAILED"
        session.commit()
    report.eligible = len(best)
    if report.published == 0:
        searches_failed = report.found == 0 and any(e.startswith("SEARCH_") for e in report.errors)
        send_failed = any(not e.startswith("SEARCH_") for e in report.errors)
        report.status = "ERROR" if searches_failed or send_failed else "NO_OFFERS"
    return report


def _candidates(
    session, settings, connector, plan, now, report, dry_run, best, tried, allowance, sleep
):
    """Yield the best untried candidates, searching more until the cycle's goal is met.

    The usual slice (page 1 of this cycle's keywords) is always searched first so the
    best of it wins. Only when that does not give enough new offers does the cycle go
    to deeper pages and further keywords, up to BETA_MAX_SEARCHES_PER_CYCLE.
    """
    budget = settings.beta_max_searches_per_cycle
    initial = min(len(report.keywords), budget)
    while True:
        while len(report.searches) < budget:
            untried = [key for key in best if key not in tried]
            if len(report.searches) >= initial and len(untried) >= allowance - report.published:
                break
            if not run_search(
                session, settings, connector, plan, now, report, dry_run, best, sleep
            ):
                break
        pending = sorted(
            (row for key, row in best.items() if key not in tried),
            key=lambda row: (row[0], row[1].item_key),
            reverse=True,
        )
        if not pending:
            return
        for row in pending:
            tried.add(row[1].item_key)
            yield row
            if report.published >= allowance:
                return

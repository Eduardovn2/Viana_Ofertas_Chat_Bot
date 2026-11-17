"""Beta Shopee mode: parsing, filters, dedup, limits, dry-run and real-send flow (offline)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from app.beta import offers
from app.beta.service import in_quiet_hours, keywords_for_cycle, run_cycle
from app.core.config import Settings
from app.db import build_session_factory
from app.integrations.errors import IntegrationError, InvalidPayload
from app.integrations.shopee import ShopeeConnector
from app.integrations.telegram import TelegramPublisher, TelegramPublishError, TelegramSendResult
from app.models import Base, BetaPost

CHAT = "-1001234567890"
TOKEN = "123456:" + "A" * 35
# 15:00 in Brasília, outside the default quiet hours.
NOON = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)


def node(item_id=1, **overrides):
    data = {
        "itemId": item_id,
        "shopId": 900,
        "productName": f"Fone Bluetooth modelo {item_id}",
        "productLink": f"https://shopee.com.br/product/900/{item_id}",
        "imageUrl": f"https://cf.shopee.com.br/file/img{item_id}",
        "price": "79.9",
        "priceMin": "79.9",
        "priceMax": "79.9",
        "commissionRate": "0.08",
        "priceDiscountRate": 40,
        "ratingStar": "4.8",
        "sales": 1520,
        "shopName": "Loja Teste",
        "offerLink": f"https://s.shopee.com.br/abc{item_id}",
    }
    data.update(overrides)
    return data


def make_settings(**overrides):
    values = dict(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        shopee_secret=SecretStr("segredo-shopee"),
        beta_enabled=True,
        beta_keywords=("fone bluetooth",),
        beta_chat_id=CHAT,
        telegram_bot_token=SecretStr(TOKEN),
        telegram_allowed_chat_ids=(CHAT,),
        telegram_publisher_enabled=True,
        telegram_real_sends_authorized=True,
    )
    values.update(overrides)
    return Settings(**values)


class FakeConnector:
    def __init__(self, nodes, lookup=None):
        self.nodes = nodes
        self.lookup = lookup or {}
        self.searches = []
        self.lookups = []

    def beta_search(self, keyword, *, limit, sort_type):
        self.searches.append((keyword, limit, sort_type))
        if isinstance(self.nodes, Exception):
            raise self.nodes
        return list(self.nodes), NOON

    def beta_lookup(self, shop_id, item_id):
        self.lookups.append((shop_id, item_id))
        key = f"{shop_id}:{item_id}"
        if key in self.lookup:
            return self.lookup[key], NOON
        for row in self.nodes:
            if f"{row['shopId']}:{row['itemId']}" == key:
                return row, NOON
        return None, NOON


class FakePublisher:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send(self, chat_id, message):
        if self.error:
            raise self.error
        self.sent.append((chat_id, message))
        return TelegramSendResult(message_id=len(self.sent), chat_id=chat_id)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield build_session_factory(engine)
    engine.dispose()


def rows(factory):
    with factory() as session:
        return session.scalars(select(BetaPost).order_by(BetaPost.id)).all()


# --- parsing / filters / rendering ---------------------------------------------------------


def test_parse_offer_reads_official_fields():
    offer = offers.parse_offer(node())
    assert offer.item_key == "900:1"
    assert offer.price_min == Decimal("79.90")
    assert offer.discount_percent == 40
    assert offer.rating == Decimal("4.80")
    assert offer.sales == 1520
    assert offer.offer_link == "https://s.shopee.com.br/abc1"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"offerLink": None}, "MISSING_OFFER_LINK"),
        ({"offerLink": "https://evil.example.com/x"}, "INVALID_OFFER_LINK"),
        ({"offerLink": "https://s.shopee.com.br/segredo-shopee"}, "INVALID_OFFER_LINK"),
        ({"priceMin": None, "price": None}, "MISSING_PRICE"),
        ({"priceMin": "100", "priceMax": "50"}, "INVALID_PRICE_RANGE"),
        ({"priceDiscountRate": "40"}, "INVALID_DISCOUNT"),
        ({"ratingStar": "7"}, "INVALID_RATING"),
        ({"productName": "  "}, "MISSING_TITLE"),
        ({"shopId": "abc"}, "INVALID_ID"),
    ],
)
def test_parse_offer_rejects_bad_nodes(overrides, reason):
    with pytest.raises(offers.OfferRejected) as error:
        offers.parse_offer(node(**overrides), secret="segredo-shopee")
    assert error.value.reason == reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"priceDiscountRate": 10}, "LOW_DISCOUNT"),
        ({"ratingStar": "4.1"}, "LOW_RATING"),
        ({"ratingStar": None}, "LOW_RATING"),
        ({"sales": 3}, "LOW_SALES"),
        ({"priceMin": "5", "priceMax": "5"}, "PRICE_TOO_LOW"),
        ({"priceMin": "5000", "priceMax": "5000"}, "PRICE_TOO_HIGH"),
        ({}, None),
    ],
)
def test_filters(overrides, reason):
    assert offers.rejection_reason(offers.parse_offer(node(**overrides)), make_settings()) == reason


def test_commission_filter_only_when_configured():
    offer = offers.parse_offer(node(commissionRate="0.02"))
    assert offers.rejection_reason(offer, make_settings()) is None
    settings = make_settings(beta_min_commission_rate=Decimal("0.05"))
    assert offers.rejection_reason(offer, settings) == "LOW_COMMISSION"


def test_score_prefers_bigger_discount():
    low = offers.score(offers.parse_offer(node(priceDiscountRate=20)))
    high = offers.score(offers.parse_offer(node(priceDiscountRate=50)))
    assert high > low


def test_render_message_is_escaped_and_has_notice():
    offer = offers.parse_offer(node(productName="Cabo <USB> & carregador", priceMax="99.9"))
    message = offers.render(offer)
    assert "Cabo &lt;USB&gt; &amp; carregador" in message.text
    assert "40% OFF" in message.text
    assert "A partir de <b>R$ 79,90</b>" in message.text
    assert "⭐ 4,8 · 1,5 mil vendidos" in message.text
    assert '<a href="https://s.shopee.com.br/abc1">Comprar na Shopee</a>' in message.text
    assert offers.PRICE_NOTICE in message.text
    assert offers.ADVERTISING_LABEL in message.text
    assert message.photo_url == "https://cf.shopee.com.br/file/img1"
    assert message.parse_mode == "HTML"


def test_render_truncates_long_titles():
    message = offers.render(offers.parse_offer(node(productName="x" * 400)))
    assert "x" * 119 + "…" in message.text
    assert "x" * 121 not in message.text


# --- schedule helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_hour", "quiet"),
    # 22h–8h Brasília == 01h–11h UTC.
    [(0, False), (1, True), (5, True), (10, True), (11, False), (18, False), (23, False)],
)
def test_quiet_hours_use_brasilia_time(utc_hour, quiet):
    when = datetime(2026, 11, 17, utc_hour, tzinfo=UTC)
    assert in_quiet_hours(make_settings(), when) is quiet


def test_quiet_hours_fall_back_to_utc_minus_3_without_tzdata():
    settings = make_settings(beta_timezone="Nowhere/Invalid")
    assert in_quiet_hours(settings, datetime(2026, 11, 17, 5, tzinfo=UTC)) is True
    assert in_quiet_hours(settings, datetime(2026, 11, 17, 18, tzinfo=UTC)) is False


def test_quiet_hours_disabled_when_start_equals_end():
    settings = make_settings(beta_quiet_hours_start=0, beta_quiet_hours_end=0)
    assert in_quiet_hours(settings, datetime(2026, 11, 17, 5, tzinfo=UTC)) is False


def test_keywords_rotate_between_cycles():
    settings = make_settings(beta_keywords=("a", "b", "c"), beta_keywords_per_cycle=2)
    first = keywords_for_cycle(settings, NOON)
    second = keywords_for_cycle(settings, NOON + timedelta(minutes=15))
    assert len(first) == 2 and len(second) == 2
    assert first != second
    assert set(first) | set(second) == {"a", "b", "c"}


def test_keywords_setting_is_normalized():
    settings = make_settings(beta_keywords=("  fone   bluetooth ", "fone bluetooth", "mouse"))
    assert settings.beta_keywords == ("fone bluetooth", "mouse")
    with pytest.raises(ValueError):
        make_settings(beta_keywords=("x" * 101,))


def test_beta_settings_are_off_by_default():
    settings = Settings(_env_file=None)
    assert settings.beta_enabled is False
    assert settings.beta_dry_run is True


# --- cycles -----------------------------------------------------------------------------------


def test_disabled_paused_and_quiet_cycles_do_nothing(factory):
    connector, publisher = FakeConnector([node()]), FakePublisher()
    with factory() as session:
        assert (
            run_cycle(
                session, make_settings(beta_enabled=False), connector, publisher, now=NOON
            ).status
            == "DISABLED"
        )
        assert (
            run_cycle(session, make_settings(), connector, publisher, now=NOON, paused=True).status
            == "PAUSED"
        )
        night = datetime(2026, 11, 17, 4, tzinfo=UTC)
        assert run_cycle(session, make_settings(), connector, publisher, now=night).status == (
            "QUIET_HOURS"
        )
    assert connector.searches == [] and publisher.sent == [] and rows(factory) == []


def test_dry_run_records_without_calling_telegram(factory):
    connector, publisher = FakeConnector([node(1), node(2, priceDiscountRate=60)]), FakePublisher()
    settings = make_settings(beta_dry_run=True)
    with factory() as session:
        report = run_cycle(session, settings, connector, publisher, now=NOON)
    assert report.mode == "DRY_RUN" and report.published == 1 and report.eligible == 2
    assert publisher.sent == []
    [row] = rows(factory)
    assert row.status == "DRY_RUN" and row.item_key == "900:2"  # best score first
    assert connector.lookups == [("900", "2")]


def test_live_cycle_sends_best_offer_and_records_it(factory):
    connector = FakeConnector([node(1), node(2, priceDiscountRate=60), node(3, sales=1)])
    publisher = FakePublisher()
    settings = make_settings(beta_dry_run=False, beta_max_posts_per_cycle=2)
    with factory() as session:
        report = run_cycle(session, settings, connector, publisher, now=NOON)
    assert report.status == "OK" and report.mode == "LIVE" and report.published == 2
    assert report.rejected == {"LOW_SALES": 1}
    assert [chat for chat, _ in publisher.sent] == [CHAT, CHAT]
    stored = rows(factory)
    assert [(r.item_key, r.status, r.telegram_message_id) for r in stored] == [
        ("900:2", "SENT", "1"),
        ("900:1", "SENT", "2"),
    ]
    assert stored[0].sent_at is not None


def test_live_requires_chat(factory):
    with factory() as session:
        report = run_cycle(
            session,
            make_settings(beta_dry_run=False, beta_chat_id=None),
            FakeConnector([node()]),
            FakePublisher(),
            now=NOON,
        )
    assert report.status == "MISSING_CHAT"


def test_duplicates_are_skipped_unless_price_dropped(factory):
    settings = make_settings(beta_dry_run=False)
    with factory() as session:
        run_cycle(session, settings, FakeConnector([node(1)]), FakePublisher(), now=NOON)
        later = NOON + timedelta(hours=2)
        report = run_cycle(session, settings, FakeConnector([node(1)]), FakePublisher(), now=later)
        assert report.published == 0 and report.rejected == {"DUPLICATE": 1}
        cheaper = node(1, priceMin="60", priceMax="60")
        report = run_cycle(
            session,
            settings,
            FakeConnector([cheaper]),
            FakePublisher(),
            now=later + timedelta(hours=1),
        )
        assert report.published == 1
        after_cooldown = NOON + timedelta(days=8)
        report = run_cycle(
            session, settings, FakeConnector([cheaper]), FakePublisher(), now=after_cooldown
        )
        assert report.published == 1


def test_dry_run_rows_do_not_block_live_posts(factory):
    with factory() as session:
        run_cycle(session, make_settings(), FakeConnector([node(1)]), FakePublisher(), now=NOON)
        report = run_cycle(
            session,
            make_settings(beta_dry_run=False),
            FakeConnector([node(1)]),
            FakePublisher(),
            now=NOON + timedelta(minutes=15),
        )
    assert report.published == 1


def test_hourly_and_daily_limits(factory):
    settings = make_settings(beta_dry_run=False, beta_max_posts_per_hour=2)
    with factory() as session:
        for i, minutes in enumerate((0, 15)):
            run_cycle(
                session,
                settings,
                FakeConnector([node(10 + i)]),
                FakePublisher(),
                now=NOON + timedelta(minutes=minutes),
            )
        connector = FakeConnector([node(20)])
        report = run_cycle(
            session, settings, connector, FakePublisher(), now=NOON + timedelta(minutes=30)
        )
        assert report.status == "LIMIT_REACHED" and connector.searches == []
        report = run_cycle(
            session,
            settings,
            FakeConnector([node(20)]),
            FakePublisher(),
            now=NOON + timedelta(minutes=61),
        )
        assert report.published == 1
    daily = make_settings(beta_dry_run=False, beta_max_posts_per_day=3, beta_max_posts_per_hour=30)
    with factory() as session:
        report = run_cycle(
            session,
            daily,
            FakeConnector([node(30)]),
            FakePublisher(),
            now=NOON + timedelta(hours=2),
        )
    assert report.status == "LIMIT_REACHED"


def test_recheck_drops_offer_whose_price_went_up(factory):
    connector = FakeConnector(
        [node(1)], lookup={"900:1": node(1, priceMin="99.9", priceMax="99.9")}
    )
    publisher = FakePublisher()
    with factory() as session:
        report = run_cycle(
            session, make_settings(beta_dry_run=False), connector, publisher, now=NOON
        )
    assert report.published == 0 and report.rejected == {"RECHECK_PRICE_UP": 1}
    assert publisher.sent == [] and rows(factory) == []


def test_recheck_missing_item_falls_through_to_next(factory):
    connector = FakeConnector([node(1), node(2, priceDiscountRate=60)], lookup={"900:2": None})
    connector.lookup = {}
    connector.nodes = [node(1), node(2, priceDiscountRate=60)]

    def lookup(shop_id, item_id):
        connector.lookups.append((shop_id, item_id))
        return (None if item_id == "2" else node(1)), NOON

    connector.beta_lookup = lookup
    with factory() as session:
        report = run_cycle(
            session, make_settings(beta_dry_run=False), connector, FakePublisher(), now=NOON
        )
    assert report.published == 1 and report.rejected == {"RECHECK_NOT_FOUND": 1}
    assert [r.item_key for r in rows(factory)] == ["900:1"]


def test_search_error_is_retried_then_reported(factory):
    connector = FakeConnector(IntegrationError("UPSTREAM_GRAPHQL_ERROR", "x"))
    waits = []
    with factory() as session:
        report = run_cycle(
            session, make_settings(), connector, FakePublisher(), now=NOON, sleep=waits.append
        )
    assert report.status == "ERROR" and report.errors == ["SEARCH_UPSTREAM_GRAPHQL_ERROR"]
    assert len(connector.searches) == 3 and waits == [2, 5] and report.retries == 2


def test_transient_search_error_recovers_on_retry(factory):
    connector = FakeConnector([node(1)])
    original = connector.beta_search
    failures = [IntegrationError("UPSTREAM_GRAPHQL_ERROR", "x")]

    def flaky(keyword, **kwargs):
        if failures:
            connector.searches.append((keyword, kwargs))
            raise failures.pop()
        return original(keyword, **kwargs)

    connector.beta_search = flaky
    with factory() as session:
        report = run_cycle(
            session, make_settings(), connector, FakePublisher(), now=NOON, sleep=lambda _: None
        )
    assert report.status == "OK" and report.published == 1
    assert report.errors == [] and report.retries == 1


def test_disabled_connector_is_not_retried(factory):
    from app.integrations.errors import ConnectorDisabled

    connector = FakeConnector(ConnectorDisabled())
    waits = []
    with factory() as session:
        report = run_cycle(
            session, make_settings(), connector, FakePublisher(), now=NOON, sleep=waits.append
        )
    assert report.errors == ["SEARCH_CONNECTOR_DISABLED"] and waits == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (TelegramPublishError("TELEGRAM_FORBIDDEN", status=403), "FAILED"),
        (TelegramPublishError("DELIVERY_UNCERTAIN", uncertain=True), "UNCERTAIN"),
    ],
)
def test_telegram_failure_is_recorded_and_stops_cycle(factory, error, status):
    settings = make_settings(beta_dry_run=False, beta_max_posts_per_cycle=3)
    connector = FakeConnector([node(1), node(2)])
    with factory() as session:
        report = run_cycle(session, settings, connector, FakePublisher(error), now=NOON)
    assert report.status == "ERROR" and report.published == 0
    [row] = rows(factory)
    assert row.status == status and row.error_code == error.code


def test_uncertain_send_is_not_repeated(factory):
    settings = make_settings(beta_dry_run=False)
    uncertain = FakePublisher(TelegramPublishError("DELIVERY_UNCERTAIN", uncertain=True))
    with factory() as session:
        run_cycle(session, settings, FakeConnector([node(1)]), uncertain, now=NOON)
        publisher = FakePublisher()
        report = run_cycle(
            session, settings, FakeConnector([node(1)]), publisher, now=NOON + timedelta(hours=2)
        )
    assert report.rejected == {"DUPLICATE": 1} and publisher.sent == []


def test_real_publisher_gates_still_apply(factory):
    """With the Telegram flags off, a live beta cycle cannot reach the network."""
    settings = make_settings(beta_dry_run=False, telegram_real_sends_authorized=False)

    def handler(request):
        raise AssertionError("no HTTP expected")

    publisher = TelegramPublisher(
        settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with factory() as session:
        report = run_cycle(session, settings, FakeConnector([node(1)]), publisher, now=NOON)
    assert report.errors == ["TELEGRAM_SEND_NOT_AUTHORIZED"]
    assert rows(factory)[0].status == "FAILED"


def test_real_publisher_sends_photo_with_caption(factory):
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 77, "chat": {"id": int(CHAT), "type": "supergroup"}},
            },
        )

    settings = make_settings(beta_dry_run=False)
    publisher = TelegramPublisher(
        settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with factory() as session:
        report = run_cycle(session, settings, FakeConnector([node(1)]), publisher, now=NOON)
    assert report.published == 1
    [(path, payload)] = calls
    assert path.endswith("/sendPhoto")
    assert payload["chat_id"] == CHAT and payload["photo"] == "https://cf.shopee.com.br/file/img1"
    assert "Comprar na Shopee" in payload["caption"]
    assert rows(factory)[0].telegram_message_id == "77"


# --- Shopee query --------------------------------------------------------------------------------


def test_beta_query_shapes():
    search = ShopeeConnector.beta_query(keyword='fone "bt"', limit=20, sort_type=2)
    assert 'keyword:"fone \\"bt\\""' in search
    assert "sortType:2,page:1,limit:20" in search
    assert "offerLink" in search and "ratingStar" in search and "sales" in search
    lookup = ShopeeConnector.beta_query(shop_id="900", item_id="1")
    assert "shopId:900,itemId:1,page:1,limit:1" in lookup
    with pytest.raises(InvalidPayload):
        ShopeeConnector.beta_query(keyword="x", shop_id="1", item_id="2")
    with pytest.raises(InvalidPayload):
        ShopeeConnector.beta_query(keyword="x", sort_type=9)


def test_beta_search_signs_request_and_returns_nodes():
    from app.integrations.http import JsonTransport

    seen = []

    def handler(request):
        seen.append(request)
        body = {
            "data": {
                "productOfferV2": {
                    "nodes": [node(1)],
                    "pageInfo": {"page": 1, "limit": 20, "hasNextPage": False},
                }
            }
        }
        return httpx.Response(200, json=body)

    transport = JsonTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0.1
    )
    connector = ShopeeConnector(
        transport, app_id=SecretStr("app123"), secret=SecretStr("s3cr3t"), enabled=True
    )
    nodes, fetched_at = connector.beta_search("fone", limit=20, sort_type=2)
    assert nodes == [node(1)] and fetched_at.tzinfo is not None
    assert seen[0].headers["Authorization"].startswith("SHA256 Credential=app123, Timestamp=")
    item, _ = connector.beta_lookup("900", "1")
    assert item == node(1)


def test_beta_search_requires_shopee_enabled():
    from app.integrations.errors import ConnectorDisabled
    from app.integrations.http import JsonTransport

    connector = ShopeeConnector(JsonTransport(min_interval=0.1), enabled=False)
    with pytest.raises(ConnectorDisabled):
        connector.beta_search("fone")


# --- worker CLI ----------------------------------------------------------------------------------


def test_worker_pause_resume_and_health(tmp_path, monkeypatch):
    from app import beta_worker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("BETA_PAUSE_FILE", str(tmp_path / "beta.pause"))
    monkeypatch.setenv("BETA_HEALTH_FILE", str(tmp_path / "health.json"))
    assert beta_worker.main(["pause"]) == 0
    assert (tmp_path / "beta.pause").exists()
    assert beta_worker.main(["resume"]) == 0
    assert not (tmp_path / "beta.pause").exists()
    assert beta_worker.main(["health"]) == 1
    settings = Settings(_env_file=None)
    beta_worker.write_health(settings, {"heartbeat_at": datetime.now(UTC).isoformat()})
    assert beta_worker.main(["health"]) == 0
    stale = datetime.now(UTC) - timedelta(hours=2)
    beta_worker.write_health(settings, {"heartbeat_at": stale.isoformat()})
    assert beta_worker.main(["health"]) == 1


def test_worker_once_dry_run_against_database(tmp_path, monkeypatch, capsys):
    from app import beta_worker

    database = tmp_path / "beta.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{database}",
        "BETA_ENABLED": "true",
        "BETA_KEYWORDS": '["fone bluetooth"]',
        "BETA_QUIET_HOURS_START": "0",
        "BETA_QUIET_HOURS_END": "0",
        "BETA_HEALTH_FILE": str(tmp_path / "health.json"),
        "BETA_PAUSE_FILE": str(tmp_path / "beta.pause"),
    }.items():
        monkeypatch.setenv(key, value)
    connector = FakeConnector([node(1)])
    monkeypatch.setattr(beta_worker, "build_connector", lambda settings: connector)
    connector.transport = httpx.Client()
    assert beta_worker.main(["once"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "DRY_RUN" and output["published"] == 1
    assert beta_worker.main(["report"]) == 0
    assert "DRY_RUN" in capsys.readouterr().out

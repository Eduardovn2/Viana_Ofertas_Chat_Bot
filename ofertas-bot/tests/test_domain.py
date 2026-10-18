from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.integrations.base import PriceSnapshot
from app.integrations.mock import SimulatedPriceProvider
from app.models import Availability, Base, Platform, PriceAccess, Product, ProductPriceHistory
from app.services.monitoring import collect_price, record_price
from app.services.products import normalize_product_identity, validate_affiliate_url


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


@pytest.fixture
def product(session):
    value = Product(
        platform=Platform.MERCADO_LIVRE,
        external_id="MLB3810190843",
        name="Produto de teste",
        original_url="https://produto.mercadolivre.com.br/MLB-3810190843-produto_JM",
        monitoring_enabled=True,
        priority=False,
    )
    session.add(value)
    session.commit()
    return value


@pytest.fixture
def snapshot():
    return PriceSnapshot(
        current_price=Decimal("199.90"),
        previous_price=None,
        availability=Availability.AVAILABLE,
        price_access=PriceAccess.PUBLIC,
        source="manual_test",
        observed_at=datetime.now(UTC) - timedelta(seconds=5),
        is_simulated=False,
    )


@pytest.mark.parametrize(
    ("url", "explicit", "expected"),
    [
        ("https://produto.mercadolivre.com.br/MLB-3810190843-produto_JM", None, "MLB3810190843"),
        ("https://www.mercadolivre.com.br/MLB3810190843", "mlb-3810190843", "MLB3810190843"),
        ("https://mercadolivre.com.br/p/MLB12345678", None, None),
        ("https://mercadolivre.com.br/produto/p/MLB12345678?item_id=MLB3810190843", None, None),
        ("https://mercadolivre.com.br/produto?item_id=MLB3810190843", None, None),
        ("https://mercadolivre.com.br/p/MLB12345678", "MLB-3810190843", "MLB3810190843"),
    ],
)
def test_mercado_livre_uses_listing_path_and_not_catalog_or_query(url, explicit, expected):
    assert normalize_product_identity(Platform.MERCADO_LIVRE, url, explicit) == expected


@pytest.mark.parametrize("explicit", ["MLB999999", "ABC123", "MLB-12abc"])
def test_mercado_livre_rejects_conflicting_or_invalid_explicit_identity(explicit):
    with pytest.raises(ValueError):
        normalize_product_identity(
            Platform.MERCADO_LIVRE,
            "https://produto.mercadolivre.com.br/MLB-3810190843-produto",
            explicit,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://shopee.com.br/produto",
        "https://shopee.com.br.evil.example/produto",
        "https://evil.shopee.com.br/produto",
        "https://user:secret@shopee.com.br/produto",
        "https://shopee.com.br:8443/produto",
        "https://shopee.com.br:invalid/produto",
        "https://shopee.com.br:/produto",
        "https://shopee.com.br\\@evil.example/produto",
        "https://shopee.com.br/\nproduto",
        " https://shopee.com.br/produto",
        "https://meli.la/curto",
    ],
)
def test_original_url_rejects_unsafe_or_shortened_urls(url):
    with pytest.raises(ValueError):
        normalize_product_identity(Platform.SHOPEE, url, None)


def test_shopee_keeps_optional_manual_identity_without_inventing_one():
    url = "https://shopee.com.br/produto-i.123.456"
    assert normalize_product_identity(Platform.SHOPEE, url, None) is None
    assert normalize_product_identity(Platform.SHOPEE, url, " 123.456 ") == "123.456"
    assert (
        normalize_product_identity(Platform.SHOPEE, "https://shopee.com.br:443/produto", None)
        is None
    )


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        (Platform.MERCADO_LIVRE, "https://meli.la/abc123"),
        (Platform.MERCADO_LIVRE, "https://www.mercadolivre.com.br/produto?matt_tool=123"),
        (Platform.SHOPEE, "https://shope.ee/abc123"),
        (Platform.SHOPEE, "https://s.shopee.com.br/abc123"),
        (Platform.SHOPEE, "https://www.shopee.com.br/produto"),
    ],
)
def test_manual_affiliate_urls_are_independently_validated_and_preserved(platform, url):
    assert validate_affiliate_url(platform, url) == url


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        (Platform.SHOPEE, "https://arbitrary.shopee.com.br/abc"),
        (Platform.SHOPEE, "https://shope.ee.evil.example/abc"),
        (Platform.SHOPEE, "https://meli.la/abc"),
        (Platform.MERCADO_LIVRE, "https://foo.mercadolivre.com.br/abc"),
        (Platform.MERCADO_LIVRE, "https://user@meli.la/abc"),
        (Platform.MERCADO_LIVRE, "http://meli.la/abc"),
        (Platform.MERCADO_LIVRE, "https://meli.la:444/abc"),
    ],
)
def test_affiliate_url_rejects_cross_platform_and_non_allowlisted_hosts(platform, url):
    with pytest.raises(ValueError):
        validate_affiliate_url(platform, url)


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("0"),
        Decimal("-0.01"),
        Decimal("10.001"),
        Decimal("10000000000.00"),
        12.30,
    ],
)
@pytest.mark.parametrize("field", ["current_price", "previous_price"])
def test_invalid_prices_fail_before_a_history_is_added(session, product, snapshot, amount, field):
    with pytest.raises(ValueError):
        record_price(session, product, replace(snapshot, **{field: amount}))
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 0


def test_exclusive_price_and_unknown_previous_price_are_preserved(session, product, snapshot):
    offset = timezone(timedelta(hours=-3))
    observation = replace(
        snapshot,
        current_price=Decimal("120"),
        price_access=PriceAccess.EXCLUSIVE,
        observed_at=snapshot.observed_at.astimezone(offset),
    )
    result = record_price(session, product, observation)
    assert result.current_price == Decimal("120.00")
    assert result.previous_price is None
    assert result.price_access is PriceAccess.EXCLUSIVE
    assert result.observed_at.utcoffset() == timedelta(0)
    assert result.observed_at == observation.observed_at
    assert result.is_simulated is False


def test_unknown_and_unavailable_prices_remain_absent(session, product, snapshot):
    result = record_price(
        session,
        product,
        replace(snapshot, current_price=None, availability=Availability.UNAVAILABLE),
    )
    assert result.current_price is None
    assert result.previous_price is None
    assert result.availability is Availability.UNAVAILABLE


@pytest.mark.parametrize(
    "changes",
    [
        {"source": " "},
        {"source": "test\nsource"},
        {"source": "x" * 101},
        {"currency": "USD"},
        {"observed_at": datetime(2025, 1, 1)},
        {"observed_at": datetime.now(UTC) + timedelta(minutes=5)},
        {"availability": "public"},
        {"price_access": "invented"},
        {"is_simulated": "false"},
        {"shipping_text": "x" * 501},
        {"installment_text": 12},
    ],
)
def test_invalid_observation_metadata_is_rejected(session, product, snapshot, changes):
    with pytest.raises(ValueError):
        record_price(session, product, replace(snapshot, **changes))


def test_service_flushes_but_leaves_commit_to_caller(session, product, snapshot):
    history = record_price(session, product, snapshot)
    assert history.id is not None
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
    session.rollback()
    assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 0
    assert session.get(Product, product.id) is not None


def test_disabled_monitoring_never_calls_provider(session, product):
    class MustNotFetch:
        def fetch(self, product):
            raise AssertionError("O provedor não deve ser chamado")

    product.monitoring_enabled = False
    with pytest.raises(ValueError, match="desativado"):
        collect_price(session, product, MustNotFetch())


def test_simulation_is_predictable_marked_and_does_not_invent_previous_price(session, product):
    observed_at = datetime.now(UTC) - timedelta(seconds=5)
    provider = SimulatedPriceProvider(clock=lambda: observed_at)
    first = provider.fetch(product)
    assert first == provider.fetch(product)
    assert first.current_price == Decimal("199.90")
    assert first.previous_price is None
    assert first.source == "simulated"
    assert first.is_simulated is True
    with pytest.raises(FrozenInstanceError):
        first.source = "live"
    stored = collect_price(session, product, provider)
    assert stored.is_simulated is True
    assert stored.source == "simulated"


def test_simulated_provider_can_represent_an_exclusive_or_unknown_price(product):
    provider = SimulatedPriceProvider(
        current_price=None,
        previous_price=Decimal("300.00"),
        availability=Availability.UNKNOWN,
        price_access=PriceAccess.EXCLUSIVE,
        installment_text="Informação simulada de parcelamento",
    )
    result = provider.fetch(product)
    assert result.current_price is None
    assert result.previous_price == Decimal("300.00")
    assert result.availability is Availability.UNKNOWN
    assert result.price_access is PriceAccess.EXCLUSIVE
    assert result.installment_text == "Informação simulada de parcelamento"

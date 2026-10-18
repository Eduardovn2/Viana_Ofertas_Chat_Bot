import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Category,
    CategoryMode,
    CategorySource,
    DestinationCategory,
    DestinationChannel,
    Platform,
    Product,
    ProductCategory,
    PublicationDestination,
)
from app.services.categories import CATALOG, CategoryService, main


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def service():
    return CategoryService()


@pytest.fixture
def product(session):
    product = Product(
        name="Produto especial",
        platform=Platform.SHOPEE,
        original_url="https://shopee.com.br/exemplo",
        monitoring_enabled=True,
    )
    session.add(product)
    session.flush()
    return product


def slugs(links):
    return {link.category.slug for link in links}


def category(session, slug):
    return session.scalar(select(Category).where(Category.slug == slug))


def test_seed_is_idempotent_and_creates_disabled_internal_house_destination(session, service):
    first = service.seed(session)
    second = service.seed(session)
    assert first["categories_created"] == 9
    assert second["categories_created"] == 0
    assert first["destination_created"] is True
    assert second["destination_created"] is False
    assert session.scalar(select(func.count()).select_from(Category)) == 9
    assert session.scalar(select(func.count()).select_from(PublicationDestination)) == 1
    house = session.get(PublicationDestination, first["destination_id"])
    assert house.name == "Ofertas de Casa"
    assert house.channel is DestinationChannel.SIMULATED
    assert house.external_id == "local:ofertas-de-casa"
    assert house.enabled is False
    assert house.category_mode is CategoryMode.SELECTED
    allowed = set(
        session.scalars(
            select(Category.slug)
            .join(DestinationCategory)
            .where(DestinationCategory.destination_id == house.id)
        )
    )
    assert allowed == {slug for slug, _ in CATALOG} - {"gerais"}


def test_seed_preserves_customized_catalog_and_destination_preferences(session, service):
    first = service.seed(session)
    technology = category(session, "tecnologia")
    technology.name = "Tecnologia personalizada"
    technology.active = False
    house = session.get(PublicationDestination, first["destination_id"])
    house.name = "Meu destino renomeado"
    house.category_mode = CategoryMode.ALL
    house.enabled = True
    for link in list(house.category_links):
        session.delete(link)
    session.flush()
    service.seed(session)
    assert technology.name == "Tecnologia personalizada"
    assert technology.active is False
    assert house.name == "Meu destino renomeado"
    assert house.enabled is True
    assert house.category_mode is CategoryMode.ALL
    assert session.scalar(select(func.count()).select_from(DestinationCategory)) == 0


@pytest.mark.parametrize("channel", [DestinationChannel.TELEGRAM, DestinationChannel.WHATSAPP])
def test_seed_does_not_replace_an_existing_house_destination_in_another_channel(
    session, service, channel
):
    destination = PublicationDestination(
        name="Ofertas de Casa",
        channel=channel,
        external_id="admin-configured-destination",
        category_mode=CategoryMode.UNCONFIGURED,
        enabled=True,
    )
    session.add(destination)
    session.flush()
    result = service.seed(session)
    assert result["destination_created"] is False
    assert result["destination_id"] == destination.id
    assert destination.channel is channel
    assert destination.category_mode is CategoryMode.UNCONFIGURED
    assert destination.enabled is True
    assert session.scalar(select(func.count()).select_from(PublicationDestination)) == 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Casaco adulto", {"moda"}),
        ("CALÇA INFANTIL", {"moda", "infantil"}),
        ("Fone de ouvido GAMER", {"tecnologia", "games"}),
        ("Cafeteira elétrica", {"eletrodomesticos"}),
        ("Parafusadeira e alicate", {"materiais-ferramentas"}),
        ("Cortina para decoração", {"casa-decoracao"}),
        ("Cabeceira de cama modular", {"casa-decoracao"}),
        ("Protetor solar e perfume", {"beleza"}),
        ("Produto especial", {"gerais"}),
        ("", {"gerais"}),
    ],
)
def test_local_rules_are_deaccented_whole_word_and_multicategory(
    session, product, service, name, expected
):
    product.name = name
    links = service.classify(session, product)
    assert slugs(links) == expected
    assert all(link.source is CategorySource.RULE for link in links)
    assert all(link.explanation for link in links)


def test_description_and_explicit_public_metadata_supply_evidence(session, product, service):
    product.description = "Kit com teclado e mouse."
    product.classification_metadata = {"public": {"brand": "Nintendo"}}
    assert slugs(service.classify(session, product)) == {"tecnologia", "games"}


def test_untrusted_store_slugs_and_private_metadata_are_not_classification_evidence(
    session, product, service
):
    product.classification_metadata = {
        "store_category_slugs": ["games"],
        "store_category_trusted": False,
        "private": {"note": "smartphone Nintendo"},
    }
    assert slugs(service.classify(session, product)) == {"gerais"}


def test_trusted_store_categories_take_precedence_over_rules(session, product, service):
    product.name = "Notebook gamer"
    product.classification_metadata = {
        "store_category_slugs": ["beleza", "moda", "beleza"],
        "store_category_trusted": True,
    }
    links = service.classify(session, product)
    assert slugs(links) == {"beleza", "moda"}
    assert all(link.source is CategorySource.STORE for link in links)
    assert all(link.confidence == Decimal("0.9500") for link in links)


def test_store_trust_requires_boolean_true_and_never_comes_from_url(session, product, service):
    product.original_url = "https://shopee.com.br/tecnologia-games-Nintendo"
    product.classification_metadata = {
        "store_category_slugs": ["games"],
        "store_category_trusted": "true",
    }
    assert slugs(service.classify(session, product)) == {"gerais"}


def test_manual_correction_survives_changed_store_data_and_rules(session, product, service):
    service.seed(session)
    beauty = category(session, "beleza")
    service.set_manual(session, product, [beauty.id, beauty.id])
    product.name = "Notebook gamer"
    product.classification_metadata = {
        "store_category_slugs": ["moda"],
        "store_category_trusted": True,
    }
    links = service.classify(session, product)
    assert slugs(links) == {"beleza"}
    assert links[0].source is CategorySource.MANUAL
    assert links[0].confidence == Decimal("1")
    assert product.manual_category_override is True
    assert session.scalar(select(func.count()).select_from(ProductCategory)) == 1


def test_empty_manual_override_blocks_automatic_tags_until_explicitly_cleared(
    session, product, service
):
    product.name = "Notebook gamer"
    assert slugs(service.classify(session, product)) == {"tecnologia", "games"}
    assert service.set_manual(session, product, []) == []
    assert service.classify(session, product) == []
    assert product.manual_category_override is True
    assert slugs(service.clear_manual_override(session, product)) == {"tecnologia", "games"}
    assert product.manual_category_override is False


def test_unknown_manual_category_fails_without_deleting_existing_tags(session, product, service):
    old = service.classify(session, product)
    with pytest.raises(ValueError, match="não existem"):
        service.set_manual(session, product, [999999])
    assert slugs(service.classify(session, product)) == slugs(old)
    assert product.manual_category_override is False


def test_existing_manual_tags_survive_automatic_reclassification_even_without_override_flag(
    session, product, service
):
    service.seed(session)
    beauty = category(session, "beleza")
    session.add(
        ProductCategory(
            product_id=product.id,
            category_id=beauty.id,
            source=CategorySource.MANUAL,
            confidence=Decimal("1"),
        )
    )
    product.manual_category_override = False
    product.name = "Notebook gamer"
    assert slugs(service.classify(session, product)) == {"beleza"}


def test_reclassification_reuses_unchanged_links_and_removes_only_old_automatic_tags(
    session, product, service
):
    product.name = "Notebook"
    first = service.classify(session, product)
    assert service.classify(session, product)[0].id == first[0].id
    product.name = "Furadeira"
    assert slugs(service.classify(session, product)) == {"materiais-ferramentas"}
    assert session.scalar(select(func.count()).select_from(ProductCategory)) == 1


def test_inactive_recognized_category_is_not_changed_into_a_fallback_to_bypass_routing(
    session, product, service
):
    service.seed(session)
    category(session, "tecnologia").active = False
    product.name = "Notebook"
    links = service.classify(session, product)
    assert slugs(links) == {"tecnologia"}
    assert links[0].category.active is False


def test_custom_rules_support_admin_categories_and_reload_when_file_changes(
    session, product, tmp_path
):
    custom = Category(slug="pets", name="Pets", active=True)
    session.add(custom)
    session.flush()
    path = tmp_path / "rules.json"
    document = {"version": 1, "rules": [{"category_slug": "pets", "keywords": ["widget"]}]}
    path.write_text(json.dumps(document), encoding="utf-8")
    product.name = "Widget"
    assert slugs(CategoryService(path).classify(session, product)) == {"pets"}
    document["rules"][0]["category_slug"] = "beleza"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert slugs(CategoryService(path).classify(session, product)) == {"beleza"}


@pytest.mark.parametrize(
    "document",
    [
        {"version": 2, "rules": []},
        {"version": 1, "rules": "invalid"},
        {"version": 1, "rules": [{"category_slug": "games", "keywords": [""]}]},
        {
            "version": 1,
            "rules": [{"category_slug": "games", "keywords": ["game"], "confidence": "NaN"}],
        },
    ],
)
def test_invalid_rules_are_rejected_instead_of_inventing_categories(tmp_path, document):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        CategoryService(path)


def test_seed_cli_prints_only_public_counts_and_identifiers(tmp_path, monkeypatch, capsys):
    from app.core.config import Settings

    database_url = f"sqlite:///{(tmp_path / 'seed.db').as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        admin_api_key="secret-test-administrative-key-123456789",
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["categories", "seed"])
    assert main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["categories_created"] == 9
    assert main() == 0
    output = capsys.readouterr().out
    assert json.loads(output)["categories_created"] == 0
    assert "secret" not in output and database_url not in output

"""Local explainable categorization. No network, paid AI or store lookup occurs."""

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Category,
    CategoryMode,
    CategorySource,
    DestinationCategory,
    DestinationChannel,
    Product,
    ProductCategory,
    PublicationDestination,
)

CATALOG = (
    ("casa-decoracao", "Casa e Decoração"),
    ("materiais-ferramentas", "Materiais e Ferramentas"),
    ("eletrodomesticos", "Eletrodomésticos"),
    ("moda", "Moda"),
    ("infantil", "Infantil"),
    ("tecnologia", "Tecnologia"),
    ("games", "Games"),
    ("beleza", "Beleza"),
    ("gerais", "Gerais"),
)
DEFAULT_RULES_PATH = Path(__file__).resolve().parents[1] / "classification_rules.json"
_PUBLIC_TEXT_FIELDS = frozenset(
    {
        "public",
        "public_text",
        "public_description",
        "public_tags",
        "brand",
        "model",
        "attributes",
        "category_name",
        "store_category_names",
        "tags",
    }
)


def normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    unaccented = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[^\W_]+", unaccented, flags=re.UNICODE))


@dataclass(frozen=True)
class ClassificationRule:
    category_slug: str
    keywords: tuple[str, ...]
    confidence: Decimal


@lru_cache(maxsize=32)
def _load_rules(path: str, modified_ns: int, size: int) -> tuple[ClassificationRule, ...]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("O arquivo de regras deve ter versão 1.")
    rows = document.get("rules")
    if not isinstance(rows, list):
        raise ValueError("O arquivo de regras deve conter uma lista de regras.")
    rules = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("category_slug"), str):
            raise ValueError("Categoria de regra inválida.")
        slug = row["category_slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 100:
            raise ValueError("Slug de regra inválido.")
        words = row.get("keywords")
        if (
            not isinstance(words, list)
            or not words
            or any(not isinstance(word, str) for word in words)
        ):
            raise ValueError("Cada regra deve conter palavras-chave não vazias.")
        keywords = tuple(dict.fromkeys(normalize_text(word) for word in words))
        if not all(keywords):
            raise ValueError("Cada palavra-chave deve conter palavras ou números.")
        try:
            confidence = Decimal(str(row.get("confidence", "0.7500")))
            if not confidence.is_finite() or not 0 <= confidence <= 1:
                raise ValueError("Confiança de regra inválida.")
            if confidence != confidence.quantize(Decimal("0.0001")):
                raise ValueError("A confiança deve ter no máximo quatro casas decimais.")
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Confiança de regra inválida.") from exc
        rules.append(ClassificationRule(slug, keywords, confidence))
    return tuple(rules)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)


class CategoryService:
    def __init__(self, rules_path: str | Path | None = None):
        self.rules_path = Path(
            rules_path or os.getenv("CATEGORY_RULES_PATH") or DEFAULT_RULES_PATH
        ).resolve()
        stat = self.rules_path.stat()
        self.rules = _load_rules(str(self.rules_path), stat.st_mtime_ns, stat.st_size)

    def _ensure_catalog(self, session: Session) -> tuple[dict[str, Category], int]:
        categories = {category.slug: category for category in session.scalars(select(Category))}
        created = 0
        for slug, name in CATALOG:
            if slug in categories:
                continue
            try:
                with session.begin_nested():
                    category = Category(slug=slug, name=name, active=True)
                    session.add(category)
                    session.flush()
                created += 1
            except IntegrityError:
                category = session.scalar(select(Category).where(Category.slug == slug))
                if category is None:
                    raise
            categories[slug] = category
        return categories, created

    def seed(self, session: Session) -> dict:
        categories, created = self._ensure_catalog(session)
        query = select(PublicationDestination).where(
            (func.lower(func.trim(PublicationDestination.name)) == "ofertas de casa")
            | (
                (PublicationDestination.channel == DestinationChannel.SIMULATED)
                & (PublicationDestination.external_id == "local:ofertas-de-casa")
            )
        )
        destination = session.scalar(query)
        destination_created = False
        if destination is None:
            try:
                with session.begin_nested():
                    destination = PublicationDestination(
                        name="Ofertas de Casa",
                        channel=DestinationChannel.SIMULATED,
                        external_id="local:ofertas-de-casa",
                        enabled=False,
                        manual_approval_required=True,
                        category_mode=CategoryMode.SELECTED,
                    )
                    session.add(destination)
                    session.flush()
                    session.add_all(
                        [
                            DestinationCategory(
                                destination_id=destination.id, category_id=categories[slug].id
                            )
                            for slug, _ in CATALOG
                            if slug != "gerais"
                        ]
                    )
                    session.flush()
                destination_created = True
            except IntegrityError:
                destination = session.scalar(query)
                if destination is None:
                    raise
        session.flush()
        return {
            "categories_created": created,
            "categories_total": len(CATALOG),
            "destination_created": destination_created,
            "destination_id": destination.id,
        }

    def _links(self, session: Session, product: Product) -> list[ProductCategory]:
        return list(
            session.scalars(
                select(ProductCategory)
                .join(Category)
                .where(ProductCategory.product_id == product.id)
                .options(joinedload(ProductCategory.category))
                .execution_options(populate_existing=True)
                .order_by(Category.slug)
            )
        )

    def _lock_product(self, session: Session, product: Product) -> None:
        if product.id is None:
            raise ValueError("O produto deve estar persistido antes da classificação.")
        session.flush()
        if (
            session.scalar(
                select(Product)
                .where(Product.id == product.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            is None
        ):
            raise ValueError("Produto não encontrado.")

    def _invalidate(self, session: Session, product: Product) -> None:
        from app.services.category_routing import invalidate_ineligible_queue

        session.flush()
        session.expire(product, ["category_links"])
        invalidate_ineligible_queue(session, product_id=product.id)

    def classify(self, session: Session, product: Product) -> list[ProductCategory]:
        self._lock_product(session, product)
        links = self._links(session, product)
        manual = [link for link in links if link.source == CategorySource.MANUAL]
        if product.manual_category_override or manual:
            for link in links:
                if link.source != CategorySource.MANUAL:
                    session.delete(link)
            self._invalidate(session, product)
            return manual
        categories, _ = self._ensure_catalog(session)
        metadata = (
            product.classification_metadata
            if isinstance(product.classification_metadata, dict)
            else {}
        )
        store_slugs = metadata.get("store_category_slugs")
        trusted = metadata.get("store_category_trusted") is True and isinstance(store_slugs, list)
        selected = {}
        if trusted:
            for slug in store_slugs:
                if isinstance(slug, str) and slug in categories:
                    selected[slug] = (
                        CategorySource.STORE,
                        Decimal("0.9500"),
                        f"Categoria confiável informada pela loja: {slug}.",
                    )
        if not selected:
            texts = [product.name or "", product.description or ""]
            for key in _PUBLIC_TEXT_FIELDS:
                texts.extend(_strings(metadata.get(key)))
            text = f" {normalize_text(' '.join(texts))} "
            for rule in self.rules:
                matches = [keyword for keyword in rule.keywords if f" {keyword} " in text]
                if rule.category_slug not in categories or not matches:
                    continue
                explanation = f"Regra local por palavras inteiras: {', '.join(matches)}."[:500]
                previous = selected.get(rule.category_slug)
                if previous is None or rule.confidence > previous[1]:
                    selected[rule.category_slug] = (
                        CategorySource.RULE,
                        rule.confidence,
                        explanation,
                    )
        if not selected:
            selected["gerais"] = (
                CategorySource.RULE,
                Decimal("0.0000"),
                "Fallback Gerais: não há evidência suficiente nas informações disponíveis.",
            )
        current = {link.category_id: link for link in links}
        selected_ids = {categories[slug].id for slug in selected}
        for link in links:
            if link.category_id not in selected_ids:
                session.delete(link)
        session.flush()
        for slug, (source, confidence, explanation) in selected.items():
            category_id = categories[slug].id
            link = current.get(category_id)
            if link is None:
                link = ProductCategory(product_id=product.id, category_id=category_id)
                session.add(link)
            link.source, link.confidence, link.explanation = source, confidence, explanation
        self._invalidate(session, product)
        return self._links(session, product)

    def set_manual(
        self, session: Session, product: Product, category_ids: list[int]
    ) -> list[ProductCategory]:
        self._lock_product(session, product)
        if any(type(category_id) is not int or category_id <= 0 for category_id in category_ids):
            raise ValueError("Os identificadores de categorias devem ser inteiros positivos.")
        desired = set(category_ids)
        known = set(session.scalars(select(Category.id).where(Category.id.in_(desired))))
        if known != desired:
            raise ValueError("Uma ou mais categorias não existem.")
        links = self._links(session, product)
        current = {link.category_id: link for link in links}
        for link in links:
            if link.category_id not in desired:
                session.delete(link)
        session.flush()
        for category_id in desired:
            link = current.get(category_id)
            if link is None:
                link = ProductCategory(product_id=product.id, category_id=category_id)
                session.add(link)
            link.source = CategorySource.MANUAL
            link.confidence = Decimal("1.0000")
            link.explanation = "Categoria escolhida explicitamente pelo administrador."
        product.manual_category_override = True
        self._invalidate(session, product)
        return self._links(session, product)

    def clear_manual_override(self, session: Session, product: Product) -> list[ProductCategory]:
        self._lock_product(session, product)
        for link in self._links(session, product):
            if link.source == CategorySource.MANUAL:
                session.delete(link)
        product.manual_category_override = False
        session.flush()
        return self.classify(session, product)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Catálogo local de categorias, sem chamadas externas."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="Cria somente categorias e destino local ausentes.")
    seed.add_argument("--rules", type=Path, default=None, help="Arquivo JSON local de regras.")
    arguments = parser.parse_args()
    from app.core.config import get_settings
    from app.db import build_engine

    engine = None
    try:
        service = CategoryService(arguments.rules)
        engine = build_engine(get_settings().database_url)
        with Session(engine) as session, session.begin():
            result = service.seed(session)
        print(json.dumps(result, ensure_ascii=False))
    except (SQLAlchemyError, ValueError, OSError):
        print(
            "Seed não concluído; confira configuração, regras e migrações do banco.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

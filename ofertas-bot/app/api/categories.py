"""Category curation and destination preferences, guarded by the existing API key."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select

from app.api.products import Db, commit, get_product
from app.core.security import require_admin
from app.models import (
    Category,
    CategoryMode,
    CategorySource,
    DestinationCategory,
    ProductCategory,
    PublicationDestination,
)
from app.services.categories import CategoryService
from app.services.category_routing import invalidate_ineligible_queue

router = APIRouter(prefix="/api/v1", tags=["Categorias"], dependencies=[Depends(require_admin)])


class CategoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    active: bool = True


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    active: bool | None = None

    @model_validator(mode="after")
    def no_required_nulls(self):
        if any(getattr(self, key) is None for key in self.model_fields_set - {"description"}):
            raise ValueError("Nome e ativo não podem ser nulos")
        return self


class CategoryRead(CategoryWrite):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
    updated_at: datetime


class CategorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category_ids: list[Annotated[int, Field(gt=0)]] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_categories(self):
        if len(self.category_ids) != len(set(self.category_ids)):
            raise ValueError("Não repita categorias na seleção")
        return self


class DestinationPreferences(CategorySelection):
    mode: CategoryMode

    @model_validator(mode="after")
    def consistent_mode(self):
        if self.mode != CategoryMode.SELECTED and self.category_ids:
            raise ValueError("Somente o modo selected aceita uma lista de categorias")
        return self


class ProductCategoryRead(BaseModel):
    category_id: int
    slug: str
    name: str
    active: bool
    source: CategorySource
    confidence: Decimal | None
    explanation: str | None


class ProductCategoriesRead(BaseModel):
    product_id: int
    manual_override: bool
    categories: list[ProductCategoryRead]


def category_or_404(session, category_id):
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "Categoria não encontrada")
    return category


def destination_or_404(session, destination_id):
    destination = session.get(PublicationDestination, destination_id)
    if destination is None:
        raise HTTPException(404, "Destino não encontrado")
    return destination


def validate_ids(session, category_ids):
    if not category_ids:
        return
    existing = set(session.scalars(select(Category.id).where(Category.id.in_(category_ids))))
    if existing != set(category_ids):
        raise HTTPException(404, "Uma ou mais categorias não existem")


def product_categories(session, product):
    links = session.execute(
        select(ProductCategory, Category)
        .join(Category, Category.id == ProductCategory.category_id)
        .where(ProductCategory.product_id == product.id)
        .order_by(Category.slug)
    )
    return ProductCategoriesRead(
        product_id=product.id,
        manual_override=product.manual_category_override,
        categories=[
            ProductCategoryRead(
                category_id=category.id,
                slug=category.slug,
                name=category.name,
                active=category.active,
                source=link.source,
                confidence=link.confidence,
                explanation=link.explanation,
            )
            for link, category in links
        ],
    )


@router.post("/categories/seed")
def seed_categories(session: Db):
    result = CategoryService().seed(session)
    commit(session)
    return result


@router.get("/categories", response_model=list[CategoryRead])
def list_categories(
    session: Db,
    active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    query = select(Category)
    if active is not None:
        query = query.where(Category.active == active)
    return session.scalars(query.order_by(Category.slug).offset(offset).limit(limit)).all()


@router.post("/categories", response_model=CategoryRead, status_code=201)
def create_category(data: CategoryWrite, session: Db):
    category = Category(**data.model_dump())
    session.add(category)
    commit(session)
    return category


@router.get("/categories/{category_id}", response_model=CategoryRead)
def get_category(category_id: int, session: Db):
    return category_or_404(session, category_id)


@router.patch("/categories/{category_id}", response_model=CategoryRead)
def update_category(category_id: int, data: CategoryUpdate, session: Db):
    category = category_or_404(session, category_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(category, key, value)
    session.flush()
    invalidate_ineligible_queue(session)
    commit(session)
    return category


@router.delete("/categories/{category_id}", status_code=204)
def delete_category(category_id: int, session: Db):
    session.delete(category_or_404(session, category_id))
    commit(session)
    return Response(status_code=204)


@router.get("/products/{product_id}/categories", response_model=ProductCategoriesRead)
def get_product_categories(product_id: int, session: Db):
    return product_categories(session, get_product(session, product_id))


@router.put("/products/{product_id}/categories", response_model=ProductCategoriesRead)
def set_product_categories(product_id: int, data: CategorySelection, session: Db):
    product = get_product(session, product_id)
    validate_ids(session, data.category_ids)
    CategoryService().set_manual(session, product, data.category_ids)
    invalidate_ineligible_queue(session, product_id=product.id)
    commit(session)
    return product_categories(session, product)


@router.post("/products/{product_id}/reclassify", response_model=ProductCategoriesRead)
def reclassify_product(product_id: int, session: Db):
    product = get_product(session, product_id)
    CategoryService().classify(session, product)
    invalidate_ineligible_queue(session, product_id=product.id)
    commit(session)
    return product_categories(session, product)


@router.delete(
    "/products/{product_id}/categories/manual-override", response_model=ProductCategoriesRead
)
def clear_manual_categories(product_id: int, session: Db):
    product = get_product(session, product_id)
    CategoryService().clear_manual_override(session, product)
    invalidate_ineligible_queue(session, product_id=product.id)
    commit(session)
    return product_categories(session, product)


@router.get("/destinations/{destination_id}/categories", response_model=DestinationPreferences)
def get_destination_categories(destination_id: int, session: Db):
    destination = destination_or_404(session, destination_id)
    return DestinationPreferences(
        mode=destination.category_mode,
        category_ids=list(
            session.scalars(
                select(DestinationCategory.category_id)
                .where(DestinationCategory.destination_id == destination.id)
                .order_by(DestinationCategory.category_id)
            )
        ),
    )


@router.put("/destinations/{destination_id}/categories", response_model=DestinationPreferences)
def set_destination_categories(destination_id: int, data: DestinationPreferences, session: Db):
    destination = destination_or_404(session, destination_id)
    validate_ids(session, data.category_ids)
    session.execute(
        delete(DestinationCategory).where(DestinationCategory.destination_id == destination.id)
    )
    session.flush()
    for category_id in data.category_ids:
        session.add(DestinationCategory(destination_id=destination.id, category_id=category_id))
    destination.category_mode = data.mode
    session.flush()
    session.expire(destination, ["category_links"])
    invalidate_ineligible_queue(session, destination_id=destination.id)
    commit(session)
    return get_destination_categories(destination_id, session)

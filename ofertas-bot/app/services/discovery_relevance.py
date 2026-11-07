"""Deterministic intent checks. Matching words never establishes commercial facts."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.services.categories import normalize_text
from app.services.simulation_safety import Identifier, SimulationPolicyInput


class SearchTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=100)
    intent: Literal["generic", "console", "phone", "tool", "accessory"] = "generic"
    priority: int = Field(default=1, ge=0, le=10, strict=True)

    @field_validator("query")
    @classmethod
    def clean(cls, value):
        if any(ord(c) < 32 for c in value) or not normalize_text(value):
            raise ValueError("Termo de busca inválido.")
        return value.strip()


class MockApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_score: Decimal = Field(default=Decimal("100"), ge=0, le=1000, decimal_places=2)
    max_per_category: int = Field(default=2, ge=1, le=5, strict=True)
    max_per_destination: int = Field(default=1, ge=1, le=5, strict=True)
    # No enable switch: this describes predictions in sealed offline runs only.


class DiscoveryPolicyInput(SimulationPolicyInput):
    enabled: StrictBool = False
    automatic_approval: Literal[False] = False
    searches: list[SearchTerm] = Field(min_length=1, max_length=8)
    exclusions: list[str] = Field(default_factory=list, max_length=30)
    category_priority: int = Field(default=1, ge=0, le=10, strict=True)
    page_size: int = Field(default=2, ge=1, le=5, strict=True)
    max_pages: int = Field(default=1, ge=1, le=2, strict=True)
    cooldown_seconds: int = Field(default=900, ge=900, le=86400, strict=True)
    requests_per_hour: int = Field(default=4, ge=1, le=10, strict=True)
    destinations: list[Identifier] = Field(default_factory=list, max_length=20)
    approval: MockApprovalPolicy = Field(default_factory=MockApprovalPolicy)

    @field_validator("searches")
    @classmethod
    def unique_searches(cls, value):
        if len({normalize_text(t.query) for t in value}) != len(value):
            raise ValueError("Buscas duplicadas.")
        return value

    @field_validator("exclusions")
    @classmethod
    def exclusions_normalized(cls, value):
        result = []
        for term in value:
            if not term.strip() or len(term) > 100 or any(ord(c) < 32 for c in term):
                raise ValueError("Exclusão inválida.")
            normalized = normalize_text(term)
            if not normalized or normalized in result:
                raise ValueError("Exclusão vazia ou duplicada.")
            result.append(normalized)
        return result

    @field_validator("destinations")
    @classmethod
    def valid_ids(cls, value):
        if any(type(i) is not int or i <= 0 for i in value) or len(set(value)) != len(value):
            raise ValueError("Destinos inválidos.")
        return value


def relevance(name, term: SearchTerm, exclusions, manual_decision=None):
    text = " " + normalize_text(name) + " "
    words = set(normalize_text(name).split())
    query_words = set(normalize_text(term.query).split())
    reason = None
    if any(" " + part + " " in text for part in exclusions):
        reason = "EXCLUSION"
    elif manual_decision == "REJECT":
        reason = "MANUAL_REJECT"
    accessory_words = {
        "console": {
            "cabo",
            "hdmi",
            "capa",
            "case",
            "controle",
            "suporte",
            "carregador",
            "pelicula",
            "skin",
        },
        "phone": {
            "capa",
            "pelicula",
            "case",
            "cabo",
            "carregador",
            "suporte",
            "display",
            "tela",
            "carcaca",
            "protetor",
        },
        "tool": {"broca", "brocas", "mandril", "escova", "adaptador"},
    }
    if reason is None and words.intersection(accessory_words.get(term.intent, set())):
        reason = "ACCESSORY_FOR_INTENT"
    if reason:
        return "FILTERED", Decimal("0"), reason
    if manual_decision == "ACCEPT":
        return "SELECTED", Decimal("100"), "MANUAL_ACCEPT_IDENTITY"
    if not query_words.intersection(words):
        return "FILTERED", Decimal("0"), "UNRELATED_QUERY"
    if not query_words.issubset(words):
        return "REVIEW", Decimal("30"), "INCOMPLETE_QUERY_IDENTITY"
    positive = {
        "console": {"console"},
        "phone": {"smartphone", "celular", "telefone", "iphone"},
        "tool": {"furadeira", "parafusadeira"},
    }
    if term.intent in positive and not words.intersection(positive[term.intent]):
        return "REVIEW", Decimal("40"), "INTENT_NOT_ESTABLISHED"
    return "SELECTED", Decimal("85"), "EXACT_WORDS_AND_INTENT"

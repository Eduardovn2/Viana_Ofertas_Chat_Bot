"""Shared whole-phrase filters, with optional conservative product intent checks."""

from app.services.categories import normalize_text


def matches_product(product_id, name, product_ids, terms, *, conservative=False):
    text = " " + normalize_text(name) + " "
    if product_ids or terms:
        if product_id not in product_ids and not any(" " + term + " " in text for term in terms):
            return False
    console_intent = any(
        any(model in term for model in ("playstation 5", "ps5", "xbox series")) for term in terms
    )
    if conservative and console_intent:
        accessories = (
            "cabo",
            "capa",
            "case",
            "controle",
            "pelicula",
            "suporte",
            "adaptador",
            "carregador",
            "bolsa",
            "headset",
            "adesivo",
            "skin",
            "hdmi",
            "para console",
        )
        if " console " not in text or any(" " + word + " " in text for word in accessories):
            return False
    return True

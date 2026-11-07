"""Explainable title mentions. No rule here establishes product equivalence."""

import re
import unicodedata
from decimal import Decimal

# Names recognized as text mentions, never as manufacturer authentication.
BRAND_MENTIONS = (
    "Brave Bull",
    "Logitech",
    "Thrustmaster",
    "Fanatec",
    "Moza",
    "Growth",
    "Max Titanium",
    "Integralmedica",
)
ATTRIBUTE_COVERAGE = {
    "identity": ["shopId", "itemId"],
    "listing_name": ["productName"],
    "announced_price": ["price", "priceMin", "priceMax"],
    "seller_information": ["shopName", "ratingStar", "sales", "shopType"],
    "structured_product_attributes": [],
    "unsupported_attributes": [
        "brand",
        "model",
        "quantity",
        "condition",
        "packaging",
        "kit",
        "variant",
        "price_conditions",
        "net_content",
        "edition",
    ],
    "source": "LOCAL_OFFICIAL_EXPLORER_CONTRACT",
    "contract_reference": "docs/CONTRATO_FONTES_SHOPEE_20261026.json",
    "live_schema_revalidated": False,
}


def normalized_measure(amount, unit):
    factors = {
        "mg": (Decimal("0.001"), "g"),
        "g": (Decimal(1), "g"),
        "kg": (Decimal(1000), "g"),
        "ml": (Decimal(1), "ml"),
        "l": (Decimal(1000), "ml"),
    }
    factor, base = factors[unit.casefold()]
    return (Decimal(amount) * factor).normalize(), base


def measurement_mentions(text):
    text = re.sub(r"(\d)\s*[x×]\s*(\d)", r"\1 x \2", text, flags=re.I)
    result = []
    for match in re.finditer(
        r"(?<![\w.,])(?P<amount>\d{1,7}(?:[.,]\d{1,6})?)\s*(?P<unit>mg|kg|g|ml|l)(?!\w)", text, re.I
    ):
        amount, unit = normalized_measure(match["amount"].replace(",", "."), match["unit"])
        if amount <= 0:
            continue
        prefix = text[max(0, match.start() - 25) : match.start()].casefold()
        suffix = text[match.end() : match.end() + 20].casefold()
        dose = re.search(r"(?:dose|por[cç][aã]o)\s*(?:de|:)?\s*$", prefix) or re.search(
            r"^\s*(?:por\s*|/\s*)(?:dose|por[cç][aã]o)", suffix
        )
        role = "DOSE_MENTION" if dose else "CONTENT_OR_SPECIFICATION_MENTION"
        result.append(
            dict(
                amount=format(amount, "f"),
                unit=unit,
                matched_text=match.group(),
                role=role,
                evidence_level="TITLE_INFERENCE_NOT_PROOF",
            )
        )
    return result


def title_inferences(name, target=None):
    text = unicodedata.normalize("NFC", name)
    inferred = {}

    def add(field, value, span, rule):
        entry = dict(
            value=value,
            matched_text=span,
            source_field="productName",
            rule=rule,
            evidence_level="TITLE_INFERENCE_NOT_PROOF",
        )
        if entry not in inferred.setdefault(field, []):
            inferred[field].append(entry)

    brands = list(BRAND_MENTIONS)
    if target is not None and target.brand.casefold() not in {b.casefold() for b in brands}:
        brands.append(target.brand)
    for brand in brands:
        match = re.search(r"(?<!\w)" + re.escape(brand) + r"(?!\w)", text, re.I)
        if match:
            add(
                "brand",
                brand.casefold(),
                match.group(),
                "KNOWN_NAME_MENTION_NOT_MANUFACTURER_PROOF",
            )

    model_pattern = r"(?<![\w+-])(?=[A-Za-z0-9+-]*[A-Za-z])(?=[A-Za-z0-9+-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\+)?(?![\w+-])"
    for match in re.finditer(model_pattern, text):
        token = match.group()
        if not re.fullmatch(
            r"\d+(?:gb|tb|ml|kg|mg|g|cm|mm|hz|v|w|l)|\d+x\d+(?:g|kg|ml|l)", token, re.I
        ):
            add("model", token.casefold(), token, "ALPHANUMERIC_MODEL_LIKE_TOKEN")
    if target is not None:
        match = re.search(r"(?<![\w+-])" + re.escape(target.model) + r"(?![\w+-])", text, re.I)
        if match:
            add("model", target.model, match.group(), "REQUESTED_MODEL_MENTION")

    for pattern, rule in (
        (r"\b(\d{1,4})\s*(?:unidades?|un\.?)(?!\w)", "EXPLICIT_UNIT_COUNT_MENTION"),
        (r"\b(?:kit|pack|pacote|combo)\s*(?:(?:com|de)\s*)?(\d{1,4})\b", "PACKAGE_COUNT_MENTION"),
        (r"\b(\d{1,4})\s*[x×]\s*\d+(?:[.,]\d+)?\s*(?:g|kg|mg|ml|l)\b", "MULTIPACK_COUNT_MENTION"),
    ):
        for match in re.finditer(pattern, text, re.I):
            if int(match.group(1)) > 0:
                add("quantity", int(match.group(1)), match.group(), rule)
    for match in re.finditer(
        r"\b(?:kit|combo|somente estrutura|sem banco|com banco|com assento)\b", text, re.I
    ):
        add("kit", match.group().casefold(), match.group(), "KIT_PHRASE_NOT_CONTENTS_PROOF")
    for match in re.finditer(r"\b(?:estrutura|assento|banco|volante|pedais|encosto)\b", text, re.I):
        add(
            "kit",
            match.group().casefold(),
            match.group(),
            "COMPONENT_MENTION_NOT_INCLUDED_CONTENTS",
        )
    for match in re.finditer(
        r"\b(?:pret[oa]|branc[oa]|azul|vermelh[oa]|cinza|rosa|verde)\b|\b\d+(?:[.,]\d+)?\s*(?:gb|tb|ml|kg|cm|mm|litros?)\b",
        text,
        re.I,
    ):
        add(
            "variant",
            match.group().casefold(),
            match.group(),
            "OPTION_OR_SPECIFICATION_MENTION_NOT_SELECTED_VARIANT",
        )
    for match in re.finditer(r"\bsimulador\s+(?:de\s+)?(voo|corrida)\b", text, re.I):
        add("usage", match.group(1).casefold(), match.group(), "USAGE_MENTION")
    for measure in measurement_mentions(text):
        add(
            "net_content",
            dict(amount=measure["amount"], unit=measure["unit"], role=measure["role"]),
            measure["matched_text"],
            "MEASURED_AMOUNT_MENTION_NOT_CONTENT_PROOF",
        )
    for match in re.finditer(r"\b(?:premium|comum|tradicional|standard|padr[aã]o)\b", text, re.I):
        value = match.group().casefold()
        add(
            "edition",
            "premium" if value == "premium" else "standard",
            match.group(),
            "EDITION_MENTION",
        )
    for match in re.finditer(r"\b(?:dosador|coqueteleira|pote vazio|capa|adaptador)\b", text, re.I):
        prefix = text[max(0, match.start() - 15) : match.start()].casefold()
        role = (
            "ACCESSORY_INCLUDED_OR_MENTIONED"
            if re.search(r"(?:com|acompanha|inclui|\+)\s*$", prefix)
            else "POSSIBLE_ACCESSORY_LISTING"
        )
        add("product_role", role, match.group(), "ACCESSORY_WORD_CONTEXT")
    for match in re.finditer(r"\bcreatina\b", text, re.I):
        if not any(
            h["value"] == "POSSIBLE_ACCESSORY_LISTING" for h in inferred.get("product_role", [])
        ):
            add("product_role", "PRODUCT_MENTION", match.group(), "PRODUCT_WORD_MENTION")
    return inferred

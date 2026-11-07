"""Strict, offline Shopee identity checks. Identity never proves attribution.

URLs are inspected, never rewritten or resolved. Callers retain the exact URL.
"""

import re
from urllib.parse import parse_qsl, unquote, urlsplit

from app.models import Platform
from app.services.products import normalize_product_identity

_ID = r"([1-9][0-9]{0,18})"
_PATHS = tuple(
    re.compile(pattern + r"/?\Z")
    for pattern in (
        rf"/product/{_ID}/{_ID}",
        rf"/[^/]+-i\.{_ID}\.{_ID}",
        rf"/universal-link/[^/]+-i\.{_ID}\.{_ID}",
    )
)
_SELECTORS = {
    "variation",
    "variation_id",
    "variationid",
    "searchvariation",
    "attributes",
    "modelid",
    "model_id",
    "sku",
    "sku_id",
}


def url_identity(url: str) -> tuple[str, str]:
    try:
        normalize_product_identity(Platform.SHOPEE, url, None)
        parsed = urlsplit(url)
        # Reject recursive encoding and encoded structural separators as well.
        if parsed.fragment or re.search(
            r"%(?![0-9a-fA-F]{2})|%2f|%5c|%25", parsed.path, re.IGNORECASE
        ):
            raise ValueError
        path = unquote(parsed.path, errors="strict")
        if (
            any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in path)
            or any(char in path for char in "\\?#@:%")
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError
        match = next((m for pattern in _PATHS if (m := pattern.fullmatch(path))), None)
        if match is None or any(int(v) > 2**63 - 1 for v in match.groups()):
            raise ValueError
        return match.group(1), match.group(2)
    except (ValueError, TypeError):
        raise ValueError("URL Shopee não comprova uma identidade de produto válida.") from None


def validate_target(url: str, expected: tuple[str, str], variant_id: str = "") -> bool:
    """Reject another item or a known incompatible variant; unknown variants stay unverified."""
    if url_identity(url) != expected:
        raise ValueError("O link corresponde a outro produto Shopee.")
    selectors = [
        (key.casefold(), value)
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
    ]
    selectors = [(key, value) for key, value in selectors if key in _SELECTORS]
    if variant_id and any(value != variant_id for _, value in selectors):
        raise ValueError("O link corresponde a outra variante Shopee.")
    if any(key == "attributes" or not value for key, value in selectors):
        return False
    return bool(selectors) if variant_id else not selectors


def expected_identity(product) -> tuple[str, str]:
    reference = product.external_id or ""
    if reference:
        match = re.fullmatch(rf"{_ID}:{_ID}", reference)
        if not match or any(int(v) > 2**63 - 1 for v in match.groups()):
            raise ValueError("Identidade Shopee cadastrada inválida.")
        expected = (match.group(1), match.group(2))
        try:
            original = url_identity(product.original_url)
        except ValueError:
            original = None  # An opaque legacy origin cannot establish another identity.
        if original is not None and original != expected:
            raise ValueError("Identidade cadastrada diverge da URL original Shopee.")
        return expected
    return url_identity(product.original_url)

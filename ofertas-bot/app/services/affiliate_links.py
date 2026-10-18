"""Validate recognizable listing identity; opaque short links need human verification."""

import re
from urllib.parse import unquote, urlsplit

from app.models import Platform, Product
from app.services.products import normalize_product_identity, validate_affiliate_url


def validate_link_for_product(product: Product, url: str):
    validate_affiliate_url(product.platform, url)
    host = urlsplit(url).hostname
    if product.platform == Platform.MERCADO_LIVRE and host != "meli.la":
        normalize_product_identity(product.platform, url, product.external_id)
    if product.platform == Platform.SHOPEE and host in {"shopee.com.br", "www.shopee.com.br"}:
        path = unquote(urlsplit(url).path)
        match = re.fullmatch(r"/product/([0-9]+)/([0-9]+)/?", path) or re.fullmatch(
            r"/[^/]+-i\.([0-9]+)\.([0-9]+)/?", path
        )
        if match and product.external_id and re.fullmatch(r"[0-9]+:[0-9]+", product.external_id):
            if ":".join(match.groups()) != product.external_id:
                raise ValueError("O link de afiliado corresponde a outro produto.")

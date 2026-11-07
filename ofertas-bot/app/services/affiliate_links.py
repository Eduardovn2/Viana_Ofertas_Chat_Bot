"""Validate recognizable listing identity; opaque short links need human verification."""

from urllib.parse import urlsplit

from app.models import Platform, Product
from app.services.products import normalize_product_identity, validate_affiliate_url
from app.services.shopee_identity import expected_identity, validate_target


def validate_link_for_product(product: Product, url: str):
    validate_affiliate_url(product.platform, url)
    host = urlsplit(url).hostname
    if product.platform == Platform.MERCADO_LIVRE and host != "meli.la":
        normalize_product_identity(product.platform, url, product.external_id)
    if product.platform == Platform.SHOPEE and host in {"shopee.com.br", "www.shopee.com.br"}:
        if not validate_target(url, expected_identity(product), getattr(product, "variant_id", "")):
            raise ValueError("O link não comprova a variante Shopee cadastrada.")

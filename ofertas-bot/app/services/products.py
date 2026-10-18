"""Validate store and affiliate URLs without calling external services."""

import re
from urllib.parse import SplitResult, unquote, urlsplit

from app.models import Platform

_PRODUCT_HOSTS: dict[Platform, frozenset[str]] = {
    Platform.MERCADO_LIVRE: frozenset(
        {"produto.mercadolivre.com.br", "www.mercadolivre.com.br", "mercadolivre.com.br"}
    ),
    Platform.SHOPEE: frozenset({"shopee.com.br", "www.shopee.com.br"}),
}

# Each host is deliberate. Suffix matching would also accept unauthorized subdomains.
_AFFILIATE_HOSTS: dict[Platform, frozenset[str]] = {
    Platform.MERCADO_LIVRE: frozenset(
        {
            "meli.la",
            "mercadolivre.com.br",
            "www.mercadolivre.com.br",
            "produto.mercadolivre.com.br",
        }
    ),
    Platform.SHOPEE: frozenset(
        {"shope.ee", "s.shopee.com.br", "shopee.com.br", "www.shopee.com.br"}
    ),
}
_ML_EXPLICIT_ID = re.compile(r"MLB-?([0-9]+)", re.IGNORECASE)
_ML_PATH_ID = re.compile(r"(?:^|/)MLB-?([0-9]+)(?=[/_-]|$)", re.IGNORECASE)


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> SplitResult:
    if not isinstance(url, str) or not url or url != url.strip():
        raise ValueError("A URL deve ser um texto não vazio, sem espaços nas extremidades.")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("A URL não pode conter espaços ou caracteres de controle.")
    if "\\" in url:
        raise ValueError("A URL não pode conter barras invertidas.")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL inválida.") from exc
    if parsed.scheme.lower() != "https" or host not in allowed_hosts:
        raise ValueError("Use uma URL HTTPS de um domínio brasileiro autorizado da plataforma.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("A URL não pode conter usuário ou senha.")
    if parsed.netloc.endswith(":"):
        raise ValueError("A porta HTTPS informada não pode estar vazia.")
    if port not in (None, 443):
        raise ValueError("A URL não pode usar uma porta diferente de HTTPS (443).")
    return parsed


def _hosts_for(platform: Platform, hosts: dict[Platform, frozenset[str]]) -> frozenset[str]:
    try:
        return hosts[platform]
    except (KeyError, TypeError) as exc:
        raise ValueError("Plataforma não suportada.") from exc


def normalize_product_identity(
    platform: Platform, original_url: str, external_id: str | None
) -> str | None:
    """Validate the original URL and return a known, normalized listing identifier.

    Mercado Livre catalog IDs in ``/p/MLB…`` are not listing IDs. Query-string IDs
    are not trusted as a substitute for an ID in the actual URL path. Shopee IDs
    are optional manual input in this phase; no redirect or network lookup occurs.
    """
    parsed = _validate_url(original_url, _hosts_for(platform, _PRODUCT_HOSTS))
    supplied_id = external_id.strip() if isinstance(external_id, str) else external_id
    if supplied_id == "":
        supplied_id = None
    if supplied_id is not None and not isinstance(supplied_id, str):
        raise ValueError("O identificador externo deve ser texto.")
    if supplied_id is not None and len(supplied_id) > 100:
        raise ValueError("O identificador externo deve ter no máximo 100 caracteres.")
    if platform == Platform.SHOPEE:
        if supplied_id is not None and any(
            ord(char) < 32 or ord(char) == 127 for char in supplied_id
        ):
            raise ValueError("O identificador externo contém caracteres de controle.")
        return supplied_id

    normalized_supplied: str | None = None
    if supplied_id is not None:
        match = _ML_EXPLICIT_ID.fullmatch(supplied_id)
        if match is None:
            raise ValueError("O anúncio do Mercado Livre deve ter um identificador MLB numérico.")
        normalized_supplied = f"MLB{match.group(1)}"

    path = unquote(parsed.path)
    # A product catalog is a different identity namespace from an individual ad.
    is_catalog = any(part.lower() == "p" for part in path.split("/"))
    path_ids = set() if is_catalog else {f"MLB{match}" for match in _ML_PATH_ID.findall(path)}
    if len(path_ids) > 1:
        raise ValueError("A URL contém identificadores de anúncio conflitantes.")
    path_id = next(iter(path_ids), None)
    if path_id is not None and len(path_id) > 100:
        raise ValueError("O identificador de anúncio da URL excede 100 caracteres.")
    if normalized_supplied is not None and path_id is not None and normalized_supplied != path_id:
        raise ValueError("O identificador informado diverge do anúncio presente na URL.")
    return normalized_supplied or path_id


def validate_affiliate_url(platform: Platform, url: str) -> str:
    """Validate a manually supplied affiliate URL and return it unchanged."""
    _validate_url(url, _hosts_for(platform, _AFFILIATE_HOSTS))
    return url

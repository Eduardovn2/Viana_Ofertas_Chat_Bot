"""Sonda somente leitura da API oficial do Mercado Livre (ambiente de teste).

Usa token de aplicacao (grant client_credentials), entao nao mexe no token OAuth
do usuario guardado no banco. Nao imprime segredos nem tokens.

    .venv/Scripts/python.exe scripts/probe_mercadolivre.py "vestido feminino"
"""

import sys
import time

import httpx

from app.core.config import Settings

API = "https://api.mercadolibre.com"


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "vestido feminino"
    settings = Settings()
    if not settings.mercadolivre_client_id or not settings.mercadolivre_client_secret:
        print("Defina MERCADOLIVRE_CLIENT_ID e MERCADOLIVRE_CLIENT_SECRET no .env")
        return 2
    client = httpx.Client(timeout=20, headers={"User-Agent": "ofertas-bot-probe/1.0"})
    token = client.post(
        f"{API}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.mercadolivre_client_id.get_secret_value(),
            "client_secret": settings.mercadolivre_client_secret.get_secret_value(),
        },
    )
    print("token de aplicacao:", token.status_code)
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

    def get(path: str):
        time.sleep(1.2)  # respeita limite por aplicacao
        resp = client.get(API + path, headers=headers)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None

    status, _ = get("/sites/MLB/search?q=teste&limit=1")
    print("/sites/MLB/search:", status, "(403 = busca publica bloqueada)")

    status, found = get(f"/sites/MLB/domain_discovery/search?q={query}&limit=1")
    if status != 200 or not found:
        print("domain_discovery falhou:", status)
        return 1
    category = found[0]["category_id"]
    print(f"categoria de '{query}':", category, found[0].get("category_name"))

    status, highlights = get(f"/highlights/MLB/category/{category}")
    content = (highlights or {}).get("content", [])
    print("mais vendidos:", status, len(content), "itens")
    counts = {"ITEM": 0, "PRODUCT": 0, "USER_PRODUCT": 0}
    for entry in content:
        counts[entry["type"]] = counts.get(entry["type"], 0) + 1
        if entry["type"] == "ITEM":
            continue  # /items/{id} responde 403 para anuncios de terceiros
        name = None
        if entry["type"] == "PRODUCT":
            st, product = get(f"/products/{entry['id']}")
            name = (product or {}).get("name") if st == 200 else None
        st, offers = get(f"/products/{entry['id']}/items")
        results = (offers or {}).get("results") or []
        if results:
            best = results[0]
            print(
                f"  {entry['position']:>2} {entry['type']:<12} {best['item_id']}"
                f" R$ {best.get('price')} (de {best.get('original_price')})"
                f" titulo: {name or '-- sem acesso --'}"
            )
    print("tipos:", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())

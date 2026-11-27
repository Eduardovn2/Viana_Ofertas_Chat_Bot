"""Diagnóstico somente leitura: a API de afiliados da Shopee expõe cupons?

Uso (PowerShell, na pasta ofertas-bot):
  .venv/Scripts/python.exe -m app.shopee_coupon_probe

Não publica nada e não altera o banco. Faz até 3 consultas à API oficial:
1. introspecção do esquema GraphQL (lista consultas e campos com cupom/voucher);
2. shopeeOfferV2 com termo "cupom" (campanhas oficiais da Shopee);
3. shopeeOfferV2 sem termo (campanhas mais recentes).
Nunca imprime AppId, Secret ou mensagens de erro da Shopee.
"""

import json
import re
import sys

from app.beta_worker import build_connector
from app.core.config import Settings
from app.integrations.errors import IntegrationError

_INTERESTING = re.compile(r"voucher|coupon|cupom|promo|discount|rebate|cashback", re.I)

_INTROSPECTION = (
    "query { __schema { queryType { name } mutationType { name } "
    "types { name kind fields { name args { name } } } } }"
)
_CAMPAIGN_FIELDS = (
    "offerName offerType offerLink originalLink commissionRate "
    "categoryId collectionId periodStartTime periodEndTime"
)


def _run(connector, query: str):
    try:
        return connector._request(query, cache=False, retry=False).data["data"]
    except IntegrationError as error:
        code = getattr(error, "upstream_code", None)
        print(f"  -> recusado pela Shopee (código {code or error.code})")
    except Exception as error:  # noqa: BLE001 - diagnóstico, só o tipo do erro
        print(f"  -> falhou: {type(error).__name__}")
    return None


def _introspect(connector) -> None:
    print("1) Esquema GraphQL (introspecção)")
    data = _run(connector, _INTROSPECTION)
    if not data:
        print("  A Shopee não permite introspecção nesta conta.")
        return
    schema = data["__schema"]
    roots = {
        (schema.get("queryType") or {}).get("name"),
        (schema.get("mutationType") or {}).get("name"),
    }
    for type_ in schema["types"]:
        if type_["name"].startswith("__") or not type_.get("fields"):
            continue
        names = [field["name"] for field in type_["fields"]]
        if type_["name"] in roots:
            print(f"  {type_['name']}: {', '.join(names)}")
        hits = [name for name in names if _INTERESTING.search(name)]
        if _INTERESTING.search(type_["name"]) or hits:
            print(f"  >> {type_['name']} tem campos relevantes: {', '.join(hits) or names}")


def _campaigns(connector, keyword: str | None) -> None:
    label = f'termo "{keyword}"' if keyword else "sem termo"
    print(f"\n{'2' if keyword else '3'}) shopeeOfferV2 ({label})")
    arguments = f"keyword:{json.dumps(keyword)}," if keyword else ""
    data = _run(
        connector,
        f"query {{ shopeeOfferV2({arguments}sortType:1,page:1,limit:20) "
        f"{{ nodes {{ {_CAMPAIGN_FIELDS} }} }} }}",
    )
    nodes = ((data or {}).get("shopeeOfferV2") or {}).get("nodes") or []
    if data is not None and not nodes:
        print("  nenhuma campanha retornada")
    for node in nodes:
        print(
            f"  - {node.get('offerName')!r} tipo={node.get('offerType')} "
            f"comissao={node.get('commissionRate')} link={node.get('originalLink')}"
        )


def main() -> int:
    settings = Settings()
    connector = build_connector(settings)
    if not connector.enabled or not connector.credentials_configured:
        print("Shopee desativada ou sem credenciais no .env.")
        return 1
    _introspect(connector)
    _campaigns(connector, "cupom")
    _campaigns(connector, None)
    print("\nCopie esta saída inteira e mande no chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

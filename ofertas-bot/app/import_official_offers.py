"""Submit a supplied official UTF-8 CSV to the local administrative offer workflow.

Preview is the default. --commit requests an import but never fills confirmations.
The manifest must explicitly contain the responsible party's actual confirmations.
"""

import argparse
import hashlib
import json
from pathlib import Path

import httpx

from app.core.config import Settings
from app.services.official_offer_import import OfficialExportInput


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--inbox", type=Path, help="Entrega local ao coletor do worker; sem HTTP.")
    args = parser.parse_args()
    raw = args.csv.read_bytes()
    if not 0 < len(raw) <= 1_000_000:
        parser.error("CSV deve conter de 1 a 1.000.000 bytes UTF-8.")
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    if "csv_content" in payload or "content_sha256" in payload:
        parser.error("O conteúdo e o hash são calculados a partir do CSV; remova-os do manifesto.")
    payload.update(csv_content=raw.decode("utf-8"), content_sha256=hashlib.sha256(raw).hexdigest())
    if args.inbox:
        if args.commit:
            parser.error("Use --inbox ou --commit, não ambos.")
        document = OfficialExportInput.model_validate(payload)
        if document.document_kind != "OFFICIAL_EXPORT":
            parser.error("O coletor aceita somente exportação oficial confirmada.")
        if not (
            document.confirmed_official_document
            and document.confirmed_column_semantics
            and document.confirmed_commercial_import
        ):
            parser.error("Origem, semântica e cadastro precisam estar explicitamente confirmados.")
        content = document.model_dump_json().encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        target = args.inbox.resolve() / ("commercial-" + digest + ".json")
        if not target.parent.is_dir():
            parser.error("Crie e autorize a pasta local do coletor primeiro.")
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        print(
            json.dumps(
                dict(
                    state="DELIVERED_TO_LOCAL_COLLECTOR",
                    manifest_sha256=digest,
                    external_http_calls=0,
                )
            )
        )
        return
    settings = Settings()
    if not settings.admin_api_key:
        parser.error("Chave administrativa local não configurada.")
    operation = "import" if args.commit else "preview"
    with httpx.Client(
        base_url="http://127.0.0.1:8000", trust_env=False, follow_redirects=False, timeout=60
    ) as client:
        response = client.post(
            "/api/v1/manual-offers/official-exports/" + operation,
            headers={"X-API-Key": settings.admin_api_key.get_secret_value()},
            json=payload,
        )
    print(
        json.dumps(
            dict(http_status=response.status_code, result=response.json()),
            ensure_ascii=False,
            indent=2,
        )
    )
    if response.is_error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

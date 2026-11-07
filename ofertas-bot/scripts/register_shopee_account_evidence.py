"""One-time documentary registration via localhost; never contacts Shopee."""

import argparse
import hashlib
import hmac
import json
from pathlib import Path

import httpx

from app.core.config import Settings
from app.services.shopee_evidence import AccountEvidenceInput


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--register", action="store_true", help="Explicitly persist the reviewed account proof"
    )
    args = parser.parse_args()
    settings = Settings()
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    # documented_app_id MUST come from the reviewed document, never substituted
    # from settings as a fabricated assertion of their correspondence.
    payload["source_sha256"] = hashlib.sha256(args.document.read_bytes()).hexdigest()
    evidence = AccountEvidenceInput.model_validate(payload)
    if not settings.shopee_app_id or not hmac.compare_digest(
        evidence.documented_app_id.get_secret_value(), settings.shopee_app_id.get_secret_value()
    ):
        raise SystemExit("O App ID documentado não corresponde à configuração local.")
    if not args.register:
        print(
            json.dumps(
                {
                    "status": "PREVIEW_ONLY",
                    "document_hash_calculated": True,
                    "documented_app_id_matches": True,
                    "persisted": False,
                }
            )
        )
        return
    if not settings.admin_api_key:
        raise SystemExit("Chave administrativa local ausente.")
    # No retry, redirects, proxy or external host. Never log body/headers.
    with httpx.Client(
        base_url="http://127.0.0.1:8000", trust_env=False, follow_redirects=False, timeout=10
    ) as client:
        response = client.post(
            "/api/v1/integrations/shopee/account-evidence",
            json=payload,
            headers={"X-API-Key": settings.admin_api_key.get_secret_value()},
        )
    if response.status_code != 201:
        raise SystemExit(
            f"Cadastro não confirmado: HTTP {response.status_code}; consultar estado antes de repetir."
        )
    print(
        json.dumps(
            {"status": "REGISTERED", "id": response.json()["id"], "publication_authorized": False}
        )
    )


if __name__ == "__main__":
    main()

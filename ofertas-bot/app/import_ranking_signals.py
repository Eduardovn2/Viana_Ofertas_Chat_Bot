"""Import an official metric export via the existing authenticated local API.

Defaults to preview. Never confirms document semantics on behalf of the user.
"""

import argparse
import hashlib
import json
from pathlib import Path

import httpx

from app.core.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    raw = args.csv.read_bytes()
    payload = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    if {"csv_content", "content_sha256"} & payload.keys():
        parser.error("Conteúdo e hash são calculados do arquivo, não do manifesto.")
    payload.update(csv_content=raw.decode("utf-8"), content_sha256=hashlib.sha256(raw).hexdigest())
    settings = Settings()
    if not settings.admin_api_key:
        parser.error("Chave administrativa local não configurada.")
    path = "/api/v1/shopee-discovery/ranking-signals/" + ("import" if args.persist else "preview")
    with httpx.Client(
        base_url="http://127.0.0.1:8000", trust_env=False, follow_redirects=False, timeout=20
    ) as client:
        response = client.post(
            path, json=payload, headers={"X-API-Key": settings.admin_api_key.get_secret_value()}
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

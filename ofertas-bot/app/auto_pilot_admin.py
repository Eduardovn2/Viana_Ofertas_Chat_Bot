"""Local authenticated pilot administration, independent of any AI service."""

import argparse
import json
from uuid import UUID

import httpx

from app.core.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("report", "worker", "approve", "start", "pause", "resume", "stop")
    )
    parser.add_argument("--pilot-id", type=UUID)
    parser.add_argument("--scope-sha256")
    parser.add_argument("--actor")
    parser.add_argument("--confirm-scope", action="store_true")
    parser.add_argument("--acknowledge-unconfirmed-quotas", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    pilot_id = args.pilot_id or settings.auto_pilot_id
    scope_hash = args.scope_sha256 or settings.auto_pilot_scope_sha256
    if not pilot_id or not settings.admin_api_key:
        parser.error("Informe piloto e configure a chave administrativa existente.")
    path = "/api/v1/auto-pilots-real/" + str(pilot_id)
    body = {}
    if args.action in {"approve", "start", "resume"}:
        if not scope_hash:
            parser.error("Esta operação exige o scope_sha256 exato.")
        body["scope_sha256"] = scope_hash
    if args.action == "approve":
        if not args.actor or not args.confirm_scope or not args.acknowledge_unconfirmed_quotas:
            parser.error(
                "Aprovação exige ator, --confirm-scope e --acknowledge-unconfirmed-quotas."
            )
        body.update(
            actor=args.actor,
            confirmed_pilot_scope=True,
            acknowledge_unconfirmed_quotas_and_permissions=True,
        )
    if args.action != "report":
        path += "/" + args.action
    with httpx.Client(
        base_url="http://127.0.0.1:8000", trust_env=False, follow_redirects=False, timeout=30
    ) as client:
        headers = {"X-API-Key": settings.admin_api_key.get_secret_value()}
        response = (
            client.get(path, headers=headers)
            if args.action in {"report", "worker"}
            else client.post(path, headers=headers, json=body)
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

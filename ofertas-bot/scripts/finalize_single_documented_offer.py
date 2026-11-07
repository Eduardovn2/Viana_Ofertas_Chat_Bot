"""One pinned publication through existing import + AUTO_PILOT, after authorization.

Requires a newly reviewed, current official-page observation. This command never
fetches Shopee, generates links, starts a worker or enables a global scheduler.
"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.contracts import fingerprint
from app.models import TelegramPublication
from app.services.affiliate_pipeline import validate_owned_link_receipt
from app.services.auto_pilot_real import RealAutoPilotService
from app.services.message_templates import MessageData, render_message
from app.services.official_offer_import import OfficialExportInput, _document, _row, process_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-id", required=True)
    parser.add_argument("--scope-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--authorization-confirmed", action="store_true")
    args = parser.parse_args()
    if not args.authorization_confirmed:
        raise SystemExit(
            "Exige confirmação específica da atualização e do escopo vinculado à mensagem."
        )
    data = OfficialExportInput.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    if data.document_kind != "OFFICIAL_PAGE_OBSERVATION":
        raise SystemExit("Este envio exige a observação atual da página oficial solicitada.")
    settings = Settings()
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)
    controller = RealAutoPilotService(factory, settings)
    started = False
    try:
        plan = controller.report(args.pilot_id)
        spec = plan["scope"]["plan"]
        assert plan["state"] == "PREPARED" and plan["scope_sha256"] == args.scope_sha256
        assert (
            spec["max_queries"] == 0
            and spec["max_messages"] == 1
            and spec["expected_message_sha256"]
        )
        assert plan["scope"]["chat_id"] == "-5309308689" and plan["scope"]["max_external_http"] == 1
        with factory() as session:
            now = datetime.now(UTC)
            rows, _ = _document(data, now)
            assert len(rows) == 1
            product, _, price, _ = _row(session, data, rows[0], now)
            assert product.id == 1 and product.external_id == "1018042820:22293101052"
            validate_owned_link_receipt(session, settings, product, now)
            message = render_message(MessageData(product.name, price, product.affiliate_link.url))
            assert (
                fingerprint({"chat_id": plan["scope"]["chat_id"], "message": asdict(message)})
                == spec["expected_message_sha256"]
            )
            preview = process_export(session, settings, data, persist=False, now=now)
            assert preview["ready_or_imported"] == 1 and preview["blocked"] == 0
        # This import uses existing confirmation/freshness guards; no values are
        # invented or marked current by the command itself.
        with factory() as session:
            imported = process_export(session, settings, data, persist=True)
            assert len(imported["created_offer_ids"]) == 1 and imported["blocked"] == 0
            session.commit()
        controller.approve(
            args.pilot_id,
            dict(
                actor="eduardo.viana.3",
                scope_sha256=args.scope_sha256,
                confirmed_pilot_scope=True,
                acknowledge_unconfirmed_quotas_and_permissions=True,
            ),
        )
        controller.start_authorized(args.pilot_id, args.scope_sha256)
        started = True
        controller.enqueue_documented_imports(args.pilot_id)
        queued = controller.report(args.pilot_id)
        assert (
            len(queued["queue"]) == 1
            and queued["queue"][0]["offer_id"] == imported["created_offer_ids"][0]
        )
        # No tick/discovery: only the one reserved publication in the existing outbox.
        controller.dispatch_batch(args.pilot_id)
    finally:
        try:
            if started:
                current = controller.report(args.pilot_id)
                if current["state"] not in {"COMPLETED", "STOPPED"}:
                    controller.stop(args.pilot_id)
            result = controller.report(args.pilot_id)
            with factory() as session:
                messages = [
                    dict(publication_id=row.id, status=str(row.status), message_id=row.message_id)
                    for row in session.scalars(
                        select(TelegramPublication).where(
                            TelegramPublication.approved_by == "AUTO_PILOT_RULES:" + args.pilot_id
                        )
                    )
                ]
            output = dict(
                pilot_id=args.pilot_id,
                state=result["state"],
                counts=result["counts"],
                publications=messages,
            )
            Path(".runtime/first-assisted-publication-result.json").write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(output, ensure_ascii=True))
        finally:
            controller.close()
            engine.dispose()


if __name__ == "__main__":
    main()

"""Read a preserved pilot capture and export a local, non-dispatchable Telegram preview."""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.integrations.contracts import fingerprint
from app.models import AppSetting, Product
from app.services.announcements import documented_metric_lines, render_announcement, validate_link
from app.services.ranking_signal_import import load_ranking_signals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-id", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    engine = build_engine(settings.database_url)
    try:
        with build_session_factory(engine)() as session:
            value = session.scalar(
                select(AppSetting.value).where(AppSetting.key == "auto_pilot:" + args.pilot_id)
            )
            if not value or fingerprint(value["scope"]) != value["scope_sha256"]:
                raise ValueError("Invalid preserved pilot scope")
            candidates = []
            for query in value["queries"]:
                capture = query.get("capture", {})
                if query["state"] != "SUCCEEDED" or capture.get("source") != "shopee:official_api":
                    continue
                comparison = capture["comparison"]
                if fingerprint(comparison) != capture["comparison_sha256"]:
                    raise ValueError("Preserved capture fingerprint mismatch")
                candidates.extend(
                    r for r in comparison["candidates"] if r["identity"] == args.identity
                )
            quote = max(candidates, key=lambda r: datetime.fromisoformat(r["observed_at"]))
            product = session.scalar(
                select(Product).where(
                    Product.external_id == args.identity, Product.variant_id == ""
                )
            )
            validate_link(session, settings, product, datetime.now(UTC))
            now = datetime.now(UTC)
            metrics = load_ranking_signals(session, [args.identity], now=now).get(args.identity, {})
            message = render_announcement(
                SimpleNamespace(
                    product=product,
                    offer=SimpleNamespace(current_price=Decimal(quote["announced_price"])),
                    history=SimpleNamespace(
                        evidence_metadata={
                            "announcement_only": {"template": "PRO_V2", "quote": quote}
                        }
                    ),
                    presentation_metrics=documented_metric_lines(metrics, args.identity, now),
                )
            )
            output = dict(
                template="PRO_V2",
                dispatchable=False,
                notice="Prévia local de captura preservada; não atualiza preço nem autoriza envio.",
                observed_at=quote["observed_at"],
                identity=args.identity,
                message=asdict(message),
                message_sha256=fingerprint(asdict(message)),
            )
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "message.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (args.output / "message.txt").write_text(message.text, encoding="utf-8")
        photo = (
            (
                f"<details><summary>Imagem oficial disponível — carregar somente ao abrir</summary>"
                f'<img loading="lazy" src="{escape(message.photo_url, quote=True)}" alt="Imagem do anúncio; confira versão e conteúdo"></details>'
            )
            if message.photo_url
            else ""
        )
        html = (
            """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vianinha — prévia Telegram</title><style>
body{background:#14222c;color:#eef6fb;font:16px/1.55 system-ui;margin:0;padding:30px}
main{max-width:470px;margin:auto}.notice{font-size:13px;color:#b1c3cf;margin-bottom:20px}
.bubble{background:#213847;padding:22px;border-radius:18px 18px 18px 4px;box-shadow:0 8px 30px #0004}
a{color:#71c7ff;overflow-wrap:anywhere}b{font-weight:750}img{width:100%;border-radius:12px}
summary{cursor:pointer;color:#a9d9fc;font-size:13px}.footer{font-size:12px;color:#9eb1bd;margin-top:16px}
</style><main><div class="notice">PRÉVIA LOCAL • não publicada<br>Dados preservados em: """
            + escape(quote["observed_at"])
            + """</div><div class="bubble">"""
            + photo
            + message.text.replace("\n", "<br>")
            + """</div><div class="footer">Mesmo HTML da legenda/mensagem. Tipografia e largura aproximam o Telegram; a aparência varia conforme o aplicativo.</div></main></html>"""
        )
        (args.output / "preview.html").write_text(html, encoding="utf-8")
        print(
            json.dumps(
                dict(
                    preview=str((args.output / "preview.html").resolve()),
                    photo_available=bool(message.photo_url),
                    external_http_calls=0,
                )
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

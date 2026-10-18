"""Write only the actual Quick Tunnel redirect; preserve every existing credential value."""

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key


def configure(path: Path, uri: str):
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", parsed.netloc)
        or parsed.path != "/oauth/mercadolivre/callback"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use somente a URI exata recebida do Quick Tunnel.")
    if not path.is_file():
        raise ValueError("Configure o .env local existente antes de preparar o túnel.")
    before = dotenv_values(path)
    set_key(path, "MERCADOLIVRE_REDIRECT_URI", uri, quote_mode="never")
    after = dotenv_values(path)
    if {k: v for k, v in before.items() if k != "MERCADOLIVRE_REDIRECT_URI"} != {
        k: v for k, v in after.items() if k != "MERCADOLIVRE_REDIRECT_URI"
    }:
        raise RuntimeError("Valores locais divergiram; interrompido sem exibir configurações.")
    if after.get("MERCADOLIVRE_REDIRECT_URI") != uri:
        raise RuntimeError("Não foi possível confirmar a Redirect URI exata.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", required=True)
    args = parser.parse_args()
    try:
        configure(Path(__file__).resolve().parents[1] / ".env", args.uri)
    except Exception:
        raise SystemExit(
            "Não foi possível preparar a Redirect URI; nenhum segredo foi exibido."
        ) from None
    print("MERCADOLIVRE_REDIRECT_URI preparada: " + args.uri)

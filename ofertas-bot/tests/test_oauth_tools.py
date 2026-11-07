"""Local tunnel configuration helpers; no processes or external requests are started."""

import importlib.util
from pathlib import Path

import pytest
from dotenv import dotenv_values

SPEC = importlib.util.spec_from_file_location(
    "configure_oauth_redirect",
    Path(__file__).resolve().parents[1] / "scripts/configure_oauth_redirect.py",
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
URI = "https://synthetic-fixture.trycloudflare.com/oauth/mercadolivre/callback"


def test_configures_only_redirect_and_preserves_existing_secrets_and_flags(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'MERCADOLIVRE_CLIENT_SECRET="SYNTHETIC_SECRET"\nPRICE_PROVIDER=simulated\nMERCADO_LIVRE_ENABLED=false\n',
        encoding="utf-8",
    )
    helper.configure(env, URI)
    helper.configure(env, URI)
    values = dotenv_values(env)
    assert values["MERCADOLIVRE_REDIRECT_URI"] == URI
    assert values["MERCADOLIVRE_CLIENT_SECRET"] == "SYNTHETIC_SECRET"
    assert values["MERCADO_LIVRE_ENABLED"] == "false"
    assert values["PRICE_PROVIDER"] == "simulated"
    assert env.read_text().count("MERCADOLIVRE_REDIRECT_URI=") == 1


@pytest.mark.parametrize(
    "uri",
    [
        "https://uncontrolled.example.com/oauth/mercadolivre/callback",
        "http://localhost/oauth/mercadolivre/callback",
        URI + "?secret=x",
        URI + "#x",
        URI + "/",
        "https://trycloudflare.com.evil.example/oauth/mercadolivre/callback",
    ],
)
def test_configure_rejects_unverified_origins_and_nonexact_paths(tmp_path, uri):
    env = tmp_path / ".env"
    env.write_text("SYNTHETIC_KEY=preserve\n")
    with pytest.raises(ValueError):
        helper.configure(env, uri)
    assert env.read_text() == "SYNTHETIC_KEY=preserve\n"

"""Worker collector over synthetic official manifests, never provider HTTP."""

import json
from datetime import timedelta

import pytest
import test_auto_pilot_real as real
import test_official_offer_import as exports

from app.services.commercial_collection import collect_documents

base_context = real.base_context
context = real.context
no_network = real.no_network


def policy(tmp_path, data, **changes):
    return dict(
        directory=str(tmp_path.resolve()),
        account_reference=data["account_reference"],
        source_references=[data["source_reference"]],
        **changes,
    )


def write(tmp_path, name, payload):
    (tmp_path / ("commercial-" + name + ".json")).write_text(
        json.dumps(payload, default=str), encoding="utf-8"
    )


def collect(ctx, spec, state=None):
    with ctx["factory"]() as session:
        result = collect_documents(session, ctx["settings"], spec, state, now=ctx["clock"][0])
        session.commit()
        return result


def test_blocked_source_does_not_stop_valid_rows(context, tmp_path):
    data, row = exports.document(context, listing=True)
    write(tmp_path, "1", {"source_state": "CAPTCHA"})
    bad = dict(row, item_id="999999")
    write(tmp_path, "2", exports.encoded(data, [bad, row]))
    state = collect(context, policy(tmp_path, data))
    assert state["external_http_calls"] == 0
    assert state["documents"][0]["reason"] == "CAPTCHA"
    result = state["documents"][1]
    assert result["enriched"] == 1 and result["blocked"] == 1
    assert len(result["created_offer_ids"]) == 1


def test_worker_tick_imports_and_queues_without_manual_offer_call(context, tmp_path, monkeypatch):
    data, row = exports.document(context, listing=True)
    write(tmp_path, "1", exports.encoded(data, [row]))
    real.authorized(context, operating_policy={}, commercial_collection=policy(tmp_path, data))
    monkeypatch.setattr(
        context["pilot"], "dispatch_batch", lambda _: context["pilot"].report(context["pilot_id"])
    )
    monkeypatch.setattr(context["pilot"], "discover_once", lambda _: None)
    report = context["pilot"].tick(context["pilot_id"])
    assert report["commercial_collection"]["documents"][0]["enriched"] == 1
    assert report["counts"]["publishable_queue"] >= 1


def test_restart_and_document_budget_are_persisted(context, tmp_path):
    data, row = exports.document(context, listing=True)
    write(tmp_path, "1", {"source_state": "CAPTCHA"})
    write(tmp_path, "2", exports.encoded(data, [row]))
    real.authorized(context, commercial_collection=policy(tmp_path, data, max_documents=1))
    context["pilot"].collect_commercial_evidence(context["pilot_id"])
    before = context["pilot"].report(context["pilot_id"])["commercial_collection"]
    context["clock"][0] += timedelta(seconds=31)
    context["pilot"].collect_commercial_evidence(context["pilot_id"])
    after = context["pilot"].report(context["pilot_id"])["commercial_collection"]
    assert before["documents"] == after["documents"]
    assert after["state"] == "DOCUMENT_BUDGET_EXHAUSTED"


@pytest.mark.parametrize("change", ["stale", "unconfirmed", "wrong_account", "unknown_stock"])
def test_invalid_evidence_never_enriches(context, tmp_path, change):
    data, row = exports.document(context, listing=True)
    spec = policy(tmp_path, data)
    if change == "stale":
        context["clock"][0] += timedelta(seconds=601)
    elif change == "unconfirmed":
        data["confirmed_commercial_import"] = False
    elif change == "wrong_account":
        data["account_reference"] = "OTHER_ACCOUNT"
    else:
        row["availability"] = "UNKNOWN"
    write(tmp_path, "1", exports.encoded(data, [row]))
    result = collect(context, spec)["documents"][0]
    assert not result.get("enriched")
    assert not result.get("created_offer_ids")


def test_legacy_plan_has_no_collector_and_prepared_cannot_import(context, tmp_path):
    data, row = exports.document(context)
    write(tmp_path, "1", exports.encoded(data, [row]))
    legacy = real.prepared(context)
    assert "commercial_collection" not in legacy["scope"]["plan"]
    plan = real.prepared(context, commercial_collection=policy(tmp_path, data))
    context["pilot"].collect_commercial_evidence(plan["authorization_id"])
    assert "commercial_collection" not in context["pilot"].report(plan["authorization_id"])


def test_local_producer_delivers_atomic_manifest_without_admin_or_network(
    context, tmp_path, monkeypatch, capsys
):
    from app import import_official_offers as cli

    data, row = exports.document(context, listing=True)
    payload = exports.encoded(data, [row])
    csv_path = tmp_path / "source.csv"
    csv_path.write_text(payload.pop("csv_content"), encoding="utf-8", newline="")
    payload.pop("content_sha256")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload, default=str), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["import", "--csv", str(csv_path), "--manifest", str(manifest), "--inbox", str(tmp_path)],
    )
    monkeypatch.setattr(
        cli, "Settings", lambda: pytest.fail("No credentials needed for local delivery")
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["external_http_calls"] == 0
    assert len(list(tmp_path.glob("commercial-*.json"))) == 1
    assert not list(tmp_path.glob("*.tmp"))
    result = collect(context, policy(tmp_path, data))
    assert result["documents"][0]["enriched"] == 1


def test_unchanged_file_not_reimported_after_restart(context, tmp_path):
    data, row = exports.document(context, listing=True)
    write(tmp_path, "1", exports.encoded(data, [row]))
    real.authorized(context, commercial_collection=policy(tmp_path, data))
    context["pilot"].collect_commercial_evidence(context["pilot_id"])
    first = context["pilot"].report(context["pilot_id"])["commercial_collection"]
    context["clock"][0] += timedelta(seconds=31)
    context["pilot"].collect_commercial_evidence(context["pilot_id"])
    second = context["pilot"].report(context["pilot_id"])["commercial_collection"]
    assert first["documents"] == second["documents"]

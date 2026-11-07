"""Existing real controller: one pinned message, no Shopee query allocation."""

from dataclasses import asdict
from decimal import Decimal

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_official_offer_import as exports

from app.integrations.contracts import fingerprint
from app.models import PublicationDestination
from app.services.message_templates import MessageData, render_message

base_context = real.base_context
context = real.context
no_network = real.no_network


@pytest.mark.parametrize("mismatch", [False, True])
def test_single_imported_message_with_zero_queries(context, mismatch):
    ctx = context
    data, row = exports.document(ctx, listing=True)
    exports.run(ctx, exports.encoded(data, [row]))
    message = render_message(
        MessageData(row["name"], Decimal("100.00"), "https://s.shopee.com.br/syntheticFixture")
    )
    digest = (
        fingerprint({"chat_id": offline.CHAT, "message": asdict(message)})
        if not mismatch
        else "0" * 64
    )
    real.authorized(
        ctx, max_queries=0, max_messages=1, operating_policy={}, expected_message_sha256=digest
    )
    ctx["pilot"].enqueue_documented_imports(ctx["pilot_id"])
    report = ctx["pilot"].dispatch_batch(ctx["pilot_id"])
    assert not report["queries"] and report["scope"]["max_external_http"] == 1
    assert report["counts"]["published"] == (0 if mismatch else 1)
    assert len(ctx["telegram_calls"]) == (0 if mismatch else 1)
    with ctx["factory"]() as session:
        assert session.get(PublicationDestination, ctx["destination_id"]).enabled is False

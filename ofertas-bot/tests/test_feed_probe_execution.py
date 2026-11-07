"""Single feed metadata permit; all network responses synthetic."""

import httpx
import pytest
import test_account_evidence_reuse as account
import test_auto_pilot_real as real
from sqlalchemy import select

from app.execute_feed_probe import execute
from app.models import AppSetting
from app.prepare_feed_probe import prepare
from app.services.shopee_evidence import record_account_evidence

base_context = real.base_context
context = real.context
no_network = real.no_network


@pytest.mark.parametrize("timeout", [False, True])
def test_exactly_one_feed_post_with_durable_reservation(context, timeout):
    ctx = context
    with ctx["factory"]() as session:
        record_account_evidence(
            session, ctx["settings"], account.account_document(ctx), now=ctx["clock"][0]
        )
        grant = prepare(session, ctx["settings"], now=ctx["clock"][0])
        session.commit()
    identifier = grant["scope"]["authorization_id"]
    calls = []

    def reply(request):
        calls.append(request)
        with ctx["factory"]() as session:
            value = session.scalar(
                select(AppSetting.value).where(AppSetting.key == "shopee_feed_probe:" + identifier)
            )
            assert value["state"] == "CLAIMED" and value["consumed"]
            assert value["requests_reserved"] == 1 and value["http_started"]
        if timeout:
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        return httpx.Response(200, json={"data": {"listItemFeeds": {"feeds": []}}})

    options = dict(
        actor="synthetic_owner",
        confirmed=True,
        isolated_testing=True,
        clock=lambda: ctx["clock"][0],
    )
    result = execute(
        ctx["factory"],
        ctx["settings"],
        identifier,
        grant["scope_sha256"],
        transport=httpx.MockTransport(reply),
        **options,
    )
    assert result["state"] == ("UNCERTAIN" if timeout else "SUCCEEDED")
    assert result["rows_read"] == 0 and len(calls) == 1
    with pytest.raises(ValueError):
        execute(
            ctx["factory"],
            ctx["settings"],
            identifier,
            grant["scope_sha256"],
            transport=httpx.MockTransport(reply),
            **options,
        )
    assert len(calls) == 1

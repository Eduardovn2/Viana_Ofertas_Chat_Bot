"""Activation-relative grants, with no changes to legacy signed scopes."""

import copy
from contextlib import closing
from datetime import datetime, timedelta

import pytest
import test_auto_pilot as offline
import test_auto_pilot_real as real
import test_auto_pilot_worker as workers
from sqlalchemy import select

from app.models import AppSetting, PublicationDestination
from app.services.auto_pilot import PREFIX

base_context = real.base_context
context = real.context
no_network = real.no_network


def test_ten_minutes_start_at_activation_and_survive_restart(context):
    ctx = context
    plan = real.prepared(ctx, duration_seconds=600, max_queries=3, max_messages=5)
    ctx["clock"][0] += timedelta(minutes=3)
    ctx["pilot"].approve(ctx["pilot_id"], real.approval(plan))
    started = ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    deadline = datetime.fromisoformat(started["effective_expires_at"])
    assert deadline == ctx["clock"][0] + timedelta(seconds=600)
    assert "activation_signature" not in started
    with closing(workers.replacement(ctx)) as replacement:
        ctx["clock"][0] += timedelta(seconds=599)
        assert replacement.report(ctx["pilot_id"])["state"] == "ACTIVE_REAL"
        ctx["clock"][0] += timedelta(seconds=1)
        result = replacement.report(ctx["pilot_id"])
        assert result["state"] == "COMPLETED" and result["reason"] == "DEADLINE"
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled
    assert not ctx["calls"] and not ctx["telegram_calls"]


def test_pause_resume_never_extends_duration(context):
    ctx = context
    plan = real.authorized(ctx, duration_seconds=600)
    deadline = ctx["pilot"].report(ctx["pilot_id"])["effective_expires_at"]
    ctx["clock"][0] += timedelta(seconds=100)
    ctx["pilot"].pause(ctx["pilot_id"])
    ctx["clock"][0] += timedelta(seconds=100)
    result = ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    assert result["effective_expires_at"] == deadline
    ctx["pilot"].pause(ctx["pilot_id"])
    ctx["clock"][0] += timedelta(seconds=400)
    assert ctx["pilot"].report(ctx["pilot_id"])["reason"] == "DEADLINE"
    with pytest.raises(ValueError):
        ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])


def test_late_start_cannot_shorten_or_extend_signed_duration(context):
    ctx = context
    plan = real.prepared(
        ctx, duration_seconds=600, expires_at=ctx["clock"][0] + timedelta(seconds=900)
    )
    ctx["pilot"].approve(ctx["pilot_id"], real.approval(plan))
    ctx["clock"][0] += timedelta(seconds=301)
    with pytest.raises(ValueError, match="insuficiente"):
        ctx["pilot"].start_authorized(ctx["pilot_id"], plan["scope_sha256"])
    with ctx["factory"]() as session:
        assert not session.get(PublicationDestination, ctx["destination_id"]).enabled


def test_runtime_deadline_tampering_is_rejected(context):
    ctx = context
    real.authorized(ctx, duration_seconds=600)
    with ctx["factory"]() as session:
        row = session.scalar(select(AppSetting).where(AppSetting.key == PREFIX + ctx["pilot_id"]))
        value = copy.deepcopy(row.value)
        value["runtime_expires_at"] = (ctx["clock"][0] + timedelta(hours=1)).isoformat()
        row.value = value
        session.commit()
    with pytest.raises(ValueError, match="ativação"):
        ctx["pilot"].tick(ctx["pilot_id"])
    ctx["pilot"].stop(ctx["pilot_id"])
    assert not ctx["calls"] and not ctx["telegram_calls"]


def test_duration_checked_at_last_transport_boundary(context, monkeypatch):
    ctx = context
    real.authorized(ctx, duration_seconds=600)
    offline.listings(ctx, [1])
    original = ctx["pilot"]._authorize_http

    def expire(request):
        if request.url.host == "api.telegram.org":
            ctx["clock"][0] += timedelta(seconds=600)
        return original(request)

    monkeypatch.setattr(ctx["pilot"], "_authorize_http", expire)
    result = ctx["pilot"].tick(ctx["pilot_id"])
    assert result["reason"] == "DEADLINE" and not ctx["telegram_calls"]


def test_worker_wait_respects_activation_deadline(context):
    ctx = context
    plan = real.authorized(ctx, duration_seconds=600)
    ctx["clock"][0] += timedelta(seconds=599)
    waits = []

    def advance(seconds):
        waits.append(seconds)
        ctx["clock"][0] += timedelta(seconds=seconds)

    assert workers.worker(ctx, plan).run(wait=advance)["reason"] == "DEADLINE"
    assert len(waits) == 1 and 0 < waits[0] <= 1


def test_legacy_scope_does_not_acquire_new_fields(context):
    plan = real.prepared(context)
    assert "duration_seconds" not in plan["scope"]["plan"]
    assert plan["effective_expires_at"] == plan["scope"]["plan"]["expires_at"].replace(
        "Z", "+00:00"
    )

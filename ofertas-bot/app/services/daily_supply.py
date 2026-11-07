"""Supply a daily pilot through its official captures and existing signed link service."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.errors import IntegrationError
from app.models import Platform, Product
from app.services.affiliate_pipeline import REAL_LINK_PREFIX, CommercialLinkFinalizer
from app.services.announcements import quote_from_capture, validate_link, validate_quote
from app.services.daily_operation import require_child, setting
from app.services.daily_queue import shopee_ready


def require_daily_link(session, settings, scope, now):
    from app.services.auto_pilot import PREFIX

    row = setting(session, PREFIX + scope["daily_pilot_id"])
    if row is None or row.value["state"] != "ACTIVE_REAL":
        raise ValueError("Piloto diário de links indisponível.")
    value = row.value
    _, plan = require_child(
        session,
        settings,
        value["scope"]["plan"],
        now,
        scope_hash=value["scope"]["daily_scope_sha256"],
        running=True,
    )
    reservations = value.get("daily_links", [])
    claim = next((item for item in reservations if item["id"] == scope["authorization_id"]), None)
    if (
        not claim
        or len(reservations) > plan.max_links
        or scope["max_posts"] != 1
        or claim["identity"] != scope["candidates"][0]["identity"]
    ):
        raise ValueError("Geração fora da reserva diária.")
    quote = quote_from_capture(value, claim["capture_id"], claim["quote_sha256"])
    validate_quote(quote, now)
    return plan


def enrich(controller, pilot_id):
    now = controller._now()
    with controller.factory() as session:
        row, value = controller._load(session, pilot_id, lock=True)
        scope = value["scope"]
        if (
            not scope["plan"].get("daily_grant_id")
            or value["state"] != controller.active_state
            or scope["plan"].get("commercial_mode") != "ANNOUNCEMENT_ONLY"
            or len(value["reservations"]) >= scope["plan"]["max_messages"]
        ):
            return
        _, policy = require_child(
            session,
            controller.settings,
            scope["plan"],
            now,
            scope_hash=scope["daily_scope_sha256"],
            running=True,
        )
        attempts = value.setdefault("daily_links", [])
        # Interrupted generation never gets another permit, even after restart.
        for item in attempts:
            if item["state"] == "RESERVED":
                batch = setting(session, REAL_LINK_PREFIX + item["id"], lock=True)
                if batch and batch.value["state"] == "CLAIMED":
                    batch.value = dict(
                        batch.value, state="UNCERTAIN", error_code="PROCESS_INTERRUPTED"
                    )
                item["state"] = (
                    batch.value["state"]
                    if batch and batch.value["state"] in {"SUCCEEDED", "UNCERTAIN"}
                    else "ABANDONED"
                )
        queued = {
            item["identity"]
            for item in value["queue"]
            if controller._already_queued(value, item["identity"])
        }
        attempted = {item["identity"] for item in attempts}
        ranks = {
            r["identity"]: r.get("intelligent_rank") or 1000000
            for r in value.get("opportunities", {}).get("analysis_queue", [])
        }
        captures = [
            (q["id"], quote)
            for q in reversed(value["queries"])
            if q.get("capture")
            for quote in q["capture"]["comparison"]["candidates"]
        ]
        captures.sort(key=lambda item: ranks.get(item[1]["identity"], 1000000))
        claim = None
        for capture_id, quote in captures:
            identity = quote["identity"]
            if identity in queued:
                continue
            try:
                observed, _ = validate_quote(quote, now)
            except (ValueError, TypeError, ArithmeticError):
                continue
            product = session.scalar(
                select(Product).where(
                    Product.platform == Platform.SHOPEE,
                    Product.external_id == identity,
                    Product.variant_id == "",
                )
            )
            if product and product.affiliate_link:
                try:
                    validate_link(session, controller.settings, product, now)
                    controller._append_announcement(session, value, pilot_id, capture_id, quote)
                    queued.add(identity)
                except (ValueError, IntegrationError):
                    pass
                continue
            if len(attempts) >= policy.max_links or identity in attempted:
                continue
            if not shopee_ready(session, controller.settings, policy.shopee_posts_per_minute, now):
                continue
            if setting(
                session, "shopee_link_claim:" + fingerprint({"identity": identity, "variant": ""})
            ):
                continue
            from app.services.shopee_evidence import account_evidence

            if account_evidence(session, controller.settings, now=now) is None:
                continue
            if product is None:
                product = Product(
                    platform=Platform.SHOPEE,
                    external_id=identity,
                    variant_id="",
                    name=quote["name"],
                    original_url="https://shopee.com.br/product/" + identity.replace(":", "/"),
                    monitoring_enabled=False,
                )
                session.add(product)
                session.flush()
            claim = dict(
                id=str(uuid4()),
                identity=identity,
                product_id=product.id,
                capture_id=capture_id,
                quote_sha256=fingerprint(quote),
                state="RESERVED",
                expires_at=min(observed + timedelta(seconds=600), controller._deadline(value)),
            )
            attempts.append(dict(claim, expires_at=claim["expires_at"].isoformat()))
            break
        controller._save(row, value)
        session.commit()
    if claim is None:
        return
    service = CommercialLinkFinalizer(
        controller.factory,
        controller.settings,
        clock=controller.clock,
        isolated_testing=controller.isolated_testing,
        daily_pilot_id=str(pilot_id),
        transport=controller._shopee_guard.delegate if controller.isolated_testing else None,
    )
    try:
        prepared = service.prepare_listing_links(
            claim["id"],
            [claim["product_id"]],
            actor=policy.actor,
            not_before=now,
            expires_at=claim["expires_at"],
            daily_pilot_id=pilot_id,
        )
        service.approve(
            claim["id"],
            scope_sha256=prepared["scope_sha256"],
            actor=policy.actor,
            confirmed_link_generation=True,
        )
        result = service.execute(claim["id"])
        state, reason = result["state"], result.get("error_code")
    except (ValueError, IntegrationError) as exc:
        state, reason = "BLOCKED", getattr(exc, "code", type(exc).__name__)
    with controller.factory() as session:
        row, value = controller._load(session, pilot_id, lock=True)
        item = next(item for item in value["daily_links"] if item["id"] == claim["id"])
        item.update(state=state, reason=reason)
        if state == "SUCCEEDED" and controller._active(value):
            quote = quote_from_capture(value, claim["capture_id"], claim["quote_sha256"])
            try:
                controller._append_announcement(
                    session, value, pilot_id, claim["capture_id"], quote
                )
            except (ValueError, IntegrationError):
                pass
        controller._save(row, value)
        session.commit()

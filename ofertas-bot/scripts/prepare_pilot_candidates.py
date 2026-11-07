"""Read existing production records into a local preparation queue; never enqueue sends.

Run from the repository root with PYTHONPATH=. . No provider calls or DB writes.
Historical prices are intentionally excluded from commercial form defaults.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select, text

from app.core.config import Settings
from app.db import build_engine, build_session_factory
from app.models import AffiliateLink, AppSetting, Offer, Product, ProductPriceHistory
from app.services.auto_pilot_real import RealAutoPilotService


def main():
    root = Path.cwd()
    manifest_path = root / "docs/AUTO_PILOT_REAL_PREPARADO_20261102.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["prepared"]
    env_hash = hashlib.sha256((root / ".env").read_bytes()).hexdigest()
    source_path = root / "docs/RESULTADO_BUSCA_MULTILOJA_20261031_2210.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    assert source["status"] == "SUCCEEDED" and source["result"]["http_status"] == 200
    engine = build_engine(Settings().database_url)
    factory = build_session_factory(engine)
    service = RealAutoPilotService(factory, Settings())
    candidates = {}
    try:
        with patch.object(httpx.Client, "send", side_effect=AssertionError("HTTP forbidden")):
            service._safe()  # Credentials format, transport, DB and paused scheduler only.
            with factory() as session:
                session.execute(text("SET TRANSACTION READ ONLY"))
                assert session.execute(
                    text("SELECT current_database(),current_schema()")
                ).one() == ("ofertas_bot", "public")
                row, plan = service._load(session, manifest["authorization_id"])
                assert plan["state"] == "PREPARED" and plan["approval"] is None
                assert plan["scope_sha256"] == manifest["scope_sha256"]
                assert not plan["queries"] and not plan["reservations"]
                persisted_plan_hash = hashlib.sha256(
                    json.dumps(row.value, sort_keys=True, default=str).encode()
                ).hexdigest()
                products = session.scalars(select(Product).order_by(Product.id)).all()
                links = session.scalars(select(AffiliateLink)).all()
                offers = session.scalars(select(Offer)).all()
                for product in products:
                    histories = session.scalars(
                        select(ProductPriceHistory).where(
                            ProductPriceHistory.product_id == product.id,
                            ProductPriceHistory.is_simulated.is_(False),
                        )
                    ).all()
                    real_api = [h for h in histories if h.source == "shopee:official_api"]
                    if product.platform.value != "SHOPEE" or not real_api:
                        continue
                    candidates[product.external_id] = dict(
                        product_id=product.id,
                        identity=product.external_id,
                        name=product.name,
                        sources=[
                            dict(
                                type="EXISTING_REAL_API_HISTORY",
                                history_id=h.id,
                                reference=h.source_reference,
                                observed_at=str(h.observed_at),
                            )
                            for h in real_api
                        ],
                        variant_id=product.variant_id or None,
                        documented_identity_present=any(
                            bool((h.evidence_metadata or {}).get("documented_identity"))
                            for h in histories
                        ),
                        associated_link_ids=[a.id for a in links if a.product_id == product.id],
                        offer_ids=[o.id for o in offers if o.product_id == product.id],
                    )
                for quote in source["result"]["comparison"]["candidates"]:
                    entry = candidates.setdefault(
                        quote["identity"],
                        dict(
                            product_id=None,
                            identity=quote["identity"],
                            name=quote["name"],
                            sources=[],
                            variant_id=None,
                            documented_identity_present=False,
                            associated_link_ids=[],
                            offer_ids=[],
                        ),
                    )
                    entry["sources"].append(
                        dict(
                            type="EXISTING_REAL_SEARCH",
                            reference=str(source_path.relative_to(root)),
                            source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                            observed_at=quote["observed_at"],
                        )
                    )
                for entry in candidates.values():
                    cockpit = "cockpit" in entry["name"].lower()
                    entry.update(
                        priority=1 if entry["product_id"] == 2 else (2 if not cockpit else 3),
                        selection_reason=(
                            "EXISTING_PRESERVED_LINK_AND_MATCHING_SEARCH_TERM"
                            if entry["product_id"] == 2
                            else "SIMPLER_TITLE_ONLY_NOT_DOCUMENTED"
                            if not cockpit
                            else "REAL_DISCOVERY_REQUIRES_MORE_IDENTITY_AND_KIT_EVIDENCE"
                        ),
                        within_prepared_search_terms=cockpit,
                        search_coverage_semantics="TEXTUAL_ONLY_NO_EQUIVALENCE_OR_PUBLICATION_PROOF",
                        state="AWAITING_DOCUMENTED_AND_CURRENT_COMMERCIAL_EVIDENCE",
                        intended_option_declared="AZUL" if entry["product_id"] == 2 else None,
                        preserved_link_reference=(
                            "docs/RESULTADO_GENERATE_SHORTLINK_SHOPEE.json#shortLink"
                            if entry["product_id"] == 2
                            else None
                        ),
                        ownership_declared_not_verified=entry["product_id"] == 2,
                        missing_evidence=[
                            "Documented identity: selected variant with attested facts OR whole listing with explicit same PUBLIC price and AVAILABLE status for every selectable option; never invent SKU",
                            "Existing own affiliate link with dated account/link documentary reference and listing/option binding",
                            "Current applicable PUBLIC price, payment conditions and AVAILABLE selected option; responsible, source, actual verification time and expiry <=600 seconds",
                        ],
                        current_commercial_form=dict(
                            product_id=entry["product_id"],
                            current_price=None,
                            actor=None,
                            verified=False,
                            availability="UNKNOWN",
                            price_access="UNKNOWN",
                            verified_at=None,
                            expires_at=None,
                            previous_price=None,
                            coupon_id=None,
                            documented_identity=None,
                        ),
                        form_submittable=False,
                        existing_pilot_requirements=[
                            "VARIANT requires documented identifier and facts; LISTING_ALL_OPTIONS allows no SKU only with explicit comprehensive commercial evidence bound to price, publication name and actual verification time",
                            "Discovery name must match the registered title or an explicitly documented alias; conflicting known facts remain blocked",
                            "Discovery of this identity inside the already approved terms; no standalone import into the publication queue",
                        ],
                        next_endpoints_after_evidence=(
                            ([] if entry["product_id"] else ["POST /api/v1/products"])
                            + [
                                "PUT /api/v1/products/{product_id}/affiliate-link",
                                "POST /api/v1/manual-offers",
                            ]
                        ),
                    )
                # A second read proves this utility never mutates the signed plan.
                session.expire(row)
                assert (
                    hashlib.sha256(
                        json.dumps(row.value, sort_keys=True, default=str).encode()
                    ).hexdigest()
                    == persisted_plan_hash
                )
                assert (
                    session.scalar(
                        select(AppSetting.value).where(AppSetting.key == "monitoring_config")
                    )["scheduler_enabled"]
                    is False
                )
                session.rollback()
            result = dict(
                checked_at=datetime.now(UTC).isoformat(),
                preparation_only=True,
                operational_writes=0,
                external_http_calls=0,
                authorization_id=manifest["authorization_id"],
                scope_sha256=manifest["scope_sha256"],
                state="PREPARED",
                plan_unchanged=True,
                counts=dict(
                    registered_real_candidates=sum(
                        e["product_id"] is not None for e in candidates.values()
                    ),
                    additional_real_search_candidates=sum(
                        e["product_id"] is None for e in candidates.values()
                    ),
                    total_candidates=len(candidates),
                    registered_offers=len(offers),
                    associated_links=len(links),
                    commercially_eligible=0,
                ),
                technical_checks="SIGNED_SCOPE_CREDENTIAL_FORMAT_DATABASE_AND_TRANSPORT_GUARDS_VALID",
                worker_requires_manual_start_inside_window=True,
                authorization_grants_no_schedule=True,
                freshness_seconds=600,
                sustained_pilot_requires_fresh_evidence_not_initial_two_hour_attestation=True,
                candidates=sorted(
                    candidates.values(), key=lambda e: (e["priority"], e["identity"])
                ),
            )
            # Do not infer an eligible count if this previously empty inventory changes.
            assert not offers, (
                "Offers now exist: evaluate using the actual eligibility service before reporting"
            )
            output = root / "docs/AUTO_PILOT_CANDIDATOS_REAIS_PENDENTES.json"
            output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps({k: v for k, v in result.items() if k != "candidates"}, indent=2))
    finally:
        service.close()
        engine.dispose()
        assert hashlib.sha256((root / ".env").read_bytes()).hexdigest() == env_hash


if __name__ == "__main__":
    main()

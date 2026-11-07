"""Local official-document adapter. No browser, network or inferred verification.

A producer with an independently authorized source writes complete importer
manifests atomically. This adapter does not claim that Shopee provides that feed.
"""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.integrations.errors import IntegrationError
from app.services.official_offer_import import OfficialExportInput, process_export


class CommercialCollectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["CONFIRMED_EXPORT_DIRECTORY_V1"] = "CONFIRMED_EXPORT_DIRECTORY_V1"
    directory: str = Field(min_length=1, max_length=500)
    account_reference: str = Field(min_length=1, max_length=500)
    source_references: list[str] = Field(min_length=1, max_length=20)
    max_documents: int = Field(default=20, strict=True, ge=1, le=100)
    max_documents_per_tick: int = Field(default=3, strict=True, ge=1, le=10)
    poll_interval_seconds: int = Field(default=30, strict=True, ge=30, le=600)

    @field_validator("directory")
    @classmethod
    def absolute_directory(cls, value):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Absolute commercial inbox path required")
        return str(path.resolve())


def collect_documents(session, settings, policy, previous, *, now):
    """Caller holds the pilot row lock and commits imports and journal together.

    Budget and content identities survive worker restarts. Unknown/blocked files
    never become evidence; one failed file does not stop other candidates.
    """
    policy = CommercialCollectionPolicy.model_validate(policy)
    state = dict(previous or {})
    state.setdefault("documents", [])
    state["external_http_calls"] = 0
    if state.get("polled_at") and now < datetime.fromisoformat(state["polled_at"]) + timedelta(
        seconds=policy.poll_interval_seconds
    ):
        return state
    state["polled_at"] = now.isoformat()
    root = Path(policy.directory)
    if not root.is_dir():
        state["state"] = "SOURCE_DIRECTORY_UNAVAILABLE"
        return state
    seen = {r["manifest_sha256"] for r in state["documents"]}
    remaining = min(policy.max_documents - len(seen), policy.max_documents_per_tick)
    state["state"] = "DOCUMENT_BUDGET_EXHAUSTED" if remaining <= 0 else "WAITING_FOR_DOCUMENTS"
    # Bound directory scanning too; producers should archive acknowledged files.
    try:
        paths = []
        for path in root.iterdir():
            if len(paths) >= 1000:
                break
            if path.name.startswith("commercial-") and path.suffix == ".json":
                paths.append(path)
    except OSError:
        state["state"] = "SOURCE_DIRECTORY_UNAVAILABLE"
        return state
    for path in sorted(paths):
        if remaining <= 0:
            break
        outcome = None
        try:
            if path.is_symlink() or path.resolve().parent != root.resolve():
                continue
            if path.stat().st_size > 1_100_000:
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            remaining -= 1
            outcome = dict(manifest_sha256=digest, at=now.isoformat(), state="CLAIMED")
            state["documents"].append(outcome)
            payload = json.loads(raw)
            if payload.get("source_state") in {"CAPTCHA", "ACCESS_DENIED", "UNAVAILABLE"}:
                outcome.update(state="BLOCKED", reason=payload["source_state"])
                continue
            data = OfficialExportInput.model_validate(payload)
            if (
                data.document_kind != "OFFICIAL_EXPORT"
                or data.account_reference != policy.account_reference
                or data.source_reference not in policy.source_references
            ):
                outcome.update(state="BLOCKED", reason="SOURCE_NOT_AUTHORIZED_BY_SCOPE")
                continue
            with session.begin_nested():
                result = process_export(session, settings, data, persist=True, now=now)
            outcome.update(
                state="PROCESSED",
                rows=result["rows"],
                created_offer_ids=result["created_offer_ids"],
                enriched=result["ready_or_imported"],
                blocked=result["blocked"],
            )
            state["state"] = "PROCESSED"
        except (ValueError, TypeError, AttributeError, OSError, IntegrationError):
            # Do not log raw manifests, validation errors or document contents.
            if outcome is not None and outcome.get("state") == "CLAIMED":
                outcome.update(state="BLOCKED", reason="INVALID_OR_UNCONFIRMED_DOCUMENT")
    return state

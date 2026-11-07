"""Versioned operational preferences, independent of authorization and credentials."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select

from app.models import AppSetting, Category, Product, PublicationDestination
from app.services.leases import conflict_insert
from app.services.simulation_safety import Identifier

KEY = "shopee_discovery_operations"
DESTINATION_PREFIX = KEY + ":destination:"
DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "discovery_operation_defaults.json"
Seconds = Annotated[int, Field(strict=True, ge=1, le=604800)]
Limit = Annotated[int, Field(strict=True, ge=0, le=10000)]
Percent = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
Score = Annotated[Decimal, Field(ge=0, le=1000000, max_digits=9, decimal_places=2)]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")

    @model_validator(mode="after")
    def nonempty(self):
        if self.start == self.end:
            raise ValueError("Use an explicit nonempty window.")
        return self


class DestinationPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    affiliate_links_simulation_enabled: StrictBool = False
    max_affiliate_links_per_evaluation: Annotated[int, Field(strict=True, ge=0, le=10)] = 1
    timezone: str = Field(min_length=1, max_length=100)
    working_days: list[Annotated[int, Field(strict=True, ge=0, le=6)]] = Field(max_length=7)
    windows: list[Window] = Field(max_length=24)
    normal_min_discount: Percent
    normal_min_score: Score
    exceptional_enabled: StrictBool
    exceptional_min_discount: Percent
    exceptional_min_score: Score
    enabled_category_ids: list[Identifier] = Field(max_length=100)
    priority_category_ids: list[Identifier] = Field(max_length=100)
    priority_product_ids: list[Identifier] = Field(max_length=1000)
    quota_period_seconds: Seconds
    max_predictions_per_period: Limit
    max_predictions_per_category_per_period: Limit
    max_predictions_per_destination_per_period: Limit

    @field_validator("timezone")
    @classmethod
    def timezone_known(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA timezone.") from None
        return value

    @field_validator(
        "working_days", "enabled_category_ids", "priority_category_ids", "priority_product_ids"
    )
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Repeated identifiers or weekdays.")
        return value

    @model_validator(mode="after")
    def meaningful_thresholds(self):
        if self.exceptional_min_discount < self.normal_min_discount:
            raise ValueError("Exceptional discount must not be below normal threshold.")
        windows = [(w.start, w.end) for w in self.windows]
        if len(windows) != len(set(windows)):
            raise ValueError("Repeated operating window.")
        return self


class OperationalPreferences(DestinationPreferences):
    discovery_interval_seconds: Seconds
    monitor_interval_seconds: Seconds
    evaluation_interval_seconds: Seconds
    max_queries_per_period: Limit
    max_queries_per_category_per_period: Limit
    max_requests_per_run: int = Field(strict=True, ge=1, le=2)
    max_results_per_evaluation: int = Field(strict=True, ge=1, le=1000)
    backoff_initial_seconds: Seconds
    backoff_max_seconds: Seconds
    lease_seconds: int = Field(strict=True, ge=30, le=600)

    @model_validator(mode="after")
    def backoff_order(self):
        if self.backoff_initial_seconds > self.backoff_max_seconds:
            raise ValueError("Initial backoff exceeds maximum.")
        return self

    def open_at(self, instant):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Timezone-aware instant required.")
        local = instant.astimezone(ZoneInfo(self.timezone))
        minute = local.hour * 60 + local.minute
        for window in self.windows:
            start = sum(
                int(v) * factor for v, factor in zip(window.start.split(":"), (60, 1), strict=True)
            )
            end = sum(
                int(v) * factor for v, factor in zip(window.end.split(":"), (60, 1), strict=True)
            )
            if start < end and local.weekday() in self.working_days and start <= minute < end:
                return True
            if start > end:
                if minute >= start and local.weekday() in self.working_days:
                    return True
                # A window crossing midnight belongs to the day on which it opened.
                if minute < end and (local - timedelta(days=1)).weekday() in self.working_days:
                    return True
        return False


class DiscoveryOperations:
    def load(self, session, destination_id=None, category_policy=None):
        defaults = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
        global_row = session.scalar(
            select(AppSetting)
            .where(AppSetting.key == KEY)
            .execution_options(populate_existing=True)
        )
        revision = global_row.value["revision"] if global_row else 0
        values = {**defaults, **(global_row.value["preferences"] if global_row else {})}
        # Retain explicitly configured Phase 4.10 policies until operators adopt
        # the versioned preferences. No previous policy is overwritten.
        if category_policy is not None:
            legacy = {
                "discovery_interval_seconds": category_policy.cooldown_seconds,
                "monitor_interval_seconds": category_policy.cooldown_seconds,
                "max_queries_per_category_per_period": category_policy.requests_per_hour,
                "normal_min_score": str(category_policy.approval.min_score),
                "max_predictions_per_category_per_period": category_policy.approval.max_per_category,
                "max_predictions_per_destination_per_period": category_policy.approval.max_per_destination,
            }
            explicit = global_row.value["preferences"] if global_row else {}
            values.update({k: v for k, v in legacy.items() if k not in explicit})
        destination_revision, overrides = 0, {}
        if destination_id is not None:
            row = session.scalar(
                select(AppSetting)
                .where(AppSetting.key == DESTINATION_PREFIX + str(destination_id))
                .execution_options(populate_existing=True)
            )
            if row:
                overrides = row.value["preferences"]
                values.update(overrides)
                destination_revision = row.value["revision"]
        return OperationalPreferences.model_validate(values), {
            "global_revision": revision,
            "destination_revision": destination_revision,
            "destination_id": destination_id,
            "overrides": overrides,
        }

    def read(self, session, destination_id=None):
        if (
            destination_id is not None
            and session.get(PublicationDestination, destination_id) is None
        ):
            raise LookupError("Destination not found.")
        preferences, metadata = self.load(session, destination_id)
        return {
            **metadata,
            "preferences": preferences.model_dump(mode="json"),
            "authorization_granted": False,
            "execution": "OFFLINE_ONLY",
            "collector_scope": "SHARED_GLOBAL",
            "destination_override_fields": sorted(DestinationPreferences.model_fields),
        }

    @staticmethod
    def _validate_references(session, values):
        for field, model in (
            ("enabled_category_ids", Category),
            ("priority_category_ids", Category),
            ("priority_product_ids", Product),
        ):
            ids = values.get(field, [])
            if set(ids) != set(session.scalars(select(model.id).where(model.id.in_(ids)))):
                raise ValueError("Unknown category or product identifier.")

    def save(self, session, changes, expected_revision, destination_id=None):
        if type(expected_revision) is not int or expected_revision < 0 or not changes:
            raise ValueError("Expected revision and nonempty preferences required.")
        if (
            destination_id is not None
            and session.get(PublicationDestination, destination_id) is None
        ):
            raise LookupError("Destination not found.")
        allowed = (
            OperationalPreferences.model_fields
            if destination_id is None
            else DestinationPreferences.model_fields
        )
        if set(changes) - set(allowed):
            raise ValueError(
                "Unknown preference; authorization and collector switches are not preferences."
            )
        key = KEY if destination_id is None else DESTINATION_PREFIX + str(destination_id)
        session.execute(
            conflict_insert(session, AppSetting)
            .values(
                key=key,
                value={"revision": 0, "preferences": {}},
                description="Offline discovery preferences; no authorization.",
            )
            .on_conflict_do_nothing(index_elements=[AppSetting.key])
        )
        row = session.scalar(
            select(AppSetting)
            .where(AppSetting.key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        before = row.value
        if before["revision"] != expected_revision:
            raise ValueError("Preference revision conflict; reload before editing.")
        merged = {**before["preferences"], **changes}
        if destination_id is not None:
            merged = {k: v for k, v in merged.items() if v is not None}
        row.value = {"revision": expected_revision + 1, "preferences": merged}
        session.flush()
        try:
            effective, _ = self.load(session, destination_id)
            self._validate_references(session, effective.model_dump())
            if destination_id is None:
                for target in session.scalars(
                    select(AppSetting).where(AppSetting.key.startswith(DESTINATION_PREFIX))
                ):
                    self.load(session, int(target.key.removeprefix(DESTINATION_PREFIX)))
        except ValidationError:
            raise ValueError(
                "Invalid operational preferences or incompatible destination override."
            ) from None
        return before, row.value, self.read(session, destination_id)

"""Confirmed official CSV observations, independent of product/offer registration."""

import csv
import hashlib
import io
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select

from app.integrations.contracts import fingerprint
from app.integrations.shopee import _positive_id
from app.models import AppSetting
from app.services.comparison import aware_utc
from app.services.leases import conflict_insert
from app.services.opportunity_ranking import MetricEvidence

PREFIX = "ranking_signal_document:"
FIELDS = {
    "shop_id",
    "item_id",
    "observed_at",
    "product_rating",
    "product_review_count",
    "sales",
    "seller_rating",
    "seller_review_count",
}


class RankingSignalImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv_content: str = Field(min_length=1, max_length=1_000_000)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    columns: dict[str, str] = Field(min_length=2, max_length=len(FIELDS))
    source_url: str
    source_reference: str = Field(min_length=3)
    origin_operation: Literal[
        "OFFICIAL_PORTAL_EXPORT", "productOfferV2", "shopOfferV2", "getItemFeedData"
    ] = "OFFICIAL_PORTAL_EXPORT"
    source_authorization_reference: str | None = Field(default=None, min_length=3)
    column_semantics_reference: str = Field(min_length=3)
    actor: str = Field(pattern=r"^[\w.@-]{3,80}$")
    exported_at: datetime
    delimiter: Literal[",", ";", "\t"] = ","
    decimal_separator: Literal[".", ","] = "."
    sales_semantics: Literal["UNSPECIFIED_PERIOD", "LIFETIME", "WINDOW"] = "UNSPECIFIED_PERIOD"
    sales_period_seconds: int | None = Field(default=None, strict=True, gt=0)
    rating_scale_max: Literal[5] | None = None
    confirmed_official_document: StrictBool = False
    confirmed_column_semantics: StrictBool = False
    confirmed_import: StrictBool = False

    @field_validator("source_url")
    @classmethod
    def official(cls, value):
        return MetricEvidence.official_source(value)

    @field_validator("exported_at")
    @classmethod
    def aware(cls, value):
        return aware_utc(value)


def import_ranking_signals(session, data, *, now, persist=False):
    data = RankingSignalImport.model_validate(data)
    now = aware_utc(now)
    if not data.confirmed_official_document or not data.confirmed_column_semantics:
        raise ValueError("Origem e semântica das novas colunas exigem confirmação explícita.")
    if persist and not data.confirmed_import:
        raise ValueError("Persistência documental exige confirmação explícita.")
    if (
        data.origin_operation != "OFFICIAL_PORTAL_EXPORT"
        and not data.source_authorization_reference
    ):
        raise ValueError(
            "Captura de operação exige referência da autorização de origem; não será executada."
        )
    if hashlib.sha256(data.csv_content.encode()).hexdigest() != data.content_sha256:
        raise ValueError("Hash do documento divergente.")
    if data.exported_at > now or data.exported_at < now - timedelta(days=30):
        raise ValueError("Data de exportação futura ou fora da janela documental.")
    if (
        set(data.columns) - FIELDS
        or not {"shop_id", "observed_at"} <= set(data.columns)
        or len(set(data.columns.values())) != len(data.columns)
    ):
        raise ValueError("Mapeamento de colunas incompleto ou ambíguo.")
    if (data.sales_semantics == "WINDOW") != (data.sales_period_seconds is not None):
        raise ValueError("Período de vendas deve corresponder à semântica declarada.")
    reader = csv.DictReader(
        io.StringIO(data.csv_content.lstrip("\ufeff")), delimiter=data.delimiter, strict=True
    )
    if (
        not reader.fieldnames
        or len(set(reader.fieldnames)) != len(reader.fieldnames)
        or set(data.columns.values()) - set(reader.fieldnames)
    ):
        raise ValueError("Colunas mapeadas ausentes ou duplicadas.")
    try:
        rows = list(reader)
    except csv.Error:
        raise ValueError("CSV inválido; nenhuma observação foi importada.") from None
    if not 1 <= len(rows) <= 200:
        raise ValueError("Documento limitado a 1–200 observações.")
    signals = []
    for index, raw in enumerate(rows, 1):
        if None in raw or None in raw.values():
            raise ValueError("Linha CSV ambígua.")
        row = {key: raw[column].strip() for key, column in data.columns.items()}
        shop = _positive_id(row["shop_id"])
        item = _positive_id(row["item_id"]) if row.get("item_id") else None
        if item is None and (
            row.get("product_rating") or row.get("sales") or data.origin_operation != "shopOfferV2"
        ):
            raise ValueError(
                "Métricas de produto exigem itemId; loja sem item só aceita métricas documentadas da loja."
            )
        observed = aware_utc(datetime.fromisoformat(row["observed_at"]))
        if observed > data.exported_at or observed < now - timedelta(days=30):
            raise ValueError("Data da observação não pertence ao documento.")
        identity = shop + ":" + item if item is not None else "shop:" + shop
        base = dict(
            source="OFFICIAL_EXPORT",
            source_url=data.source_url,
            reference=f"{data.source_reference}:row:{index}",
            source_sha256=data.content_sha256,
            actor=data.actor,
            confirmed_semantics=True,
            observed_at=observed,
            semantics_reference=data.column_semantics_reference,
        )
        for field, metric, subject, sample in (
            (
                "product_rating",
                "PRODUCT_RATING" if data.rating_scale_max == 5 else "PRODUCT_RATING_STAR_UNSCALED",
                identity,
                "product_review_count",
            ),
            (
                "sales",
                {
                    "LIFETIME": "LIFETIME_SALES",
                    "WINDOW": "WINDOW_SALES",
                    "UNSPECIFIED_PERIOD": "PRODUCT_SALES_UNSPECIFIED_PERIOD",
                }[data.sales_semantics],
                identity,
                None,
            ),
            ("seller_rating", "SELLER_RATING", shop, "seller_review_count"),
        ):
            if not row.get(field):
                continue
            if field == "seller_rating" and data.rating_scale_max != 5:
                raise ValueError("Avaliação do vendedor exige escala documentada.")
            evidence = MetricEvidence(
                **base,
                subject_identity=subject,
                metric=metric,
                value=row[field].replace(",", ".") if data.decimal_separator == "," else row[field],
                sample_count=int(row[sample]) if sample and row.get(sample) else None,
                period_seconds=data.sales_period_seconds if field == "sales" else None,
                series_key=f"{field}:{subject}:{data.sales_semantics if field == 'sales' else 'RATING'}",
            )
            signals.append(
                dict(
                    identity=identity,
                    dimension={
                        "product_rating": "reviews",
                        "sales": "popularity",
                        "seller_rating": "seller",
                    }[field],
                    evidence=evidence.model_dump(mode="json"),
                )
            )
    if not signals:
        raise ValueError('Documento sem métricas mapeadas; nenhuma observação foi gravada.')
    binding = data.model_dump(
        mode="json",
        exclude={
            "csv_content",
            "confirmed_import",
            "confirmed_official_document",
            "confirmed_column_semantics",
        },
    )
    document_id = fingerprint(binding)
    value = dict(
        document_id=document_id,
        binding=binding,
        signals=signals,
        signals_sha256=fingerprint(signals),
        state="DOCUMENTED",
    )
    if persist:
        if session is None:
            raise ValueError("Importação persistente exige sessão administrativa.")
        session.execute(
            conflict_insert(session, AppSetting)
            .values(key=PREFIX + document_id, value=value)
            .on_conflict_do_nothing(index_elements=[AppSetting.key])
        )
    return dict(
        document_id=document_id,
        observations=len(rows),
        signals=signals,
        persisted=persist,
        creates_products=False,
        creates_offers=False,
        external_calls=0,
    )


def load_ranking_signals(session, identities, *, now=None):
    """Read immutable documents. Conflicting observations never win by arrival order."""
    wanted = set(identities)
    if not wanted:
        return {}
    grouped = {}
    documents = session.scalars(
        select(AppSetting.value)
        .where(AppSetting.key.startswith(PREFIX))
        .order_by(AppSetting.id.desc())
        .limit(500)
    )
    for doc in documents:
        if (
            doc.get("state") != "DOCUMENTED"
            or fingerprint(doc.get("signals")) != doc.get("signals_sha256")
            or fingerprint(doc.get("binding")) != doc.get("document_id")
        ):
            continue
        for point in doc["signals"]:
            targets = [point["identity"]] if point["identity"] in wanted else []
            if point["identity"].startswith("shop:") and point["dimension"] == "seller":
                targets = [
                    identity
                    for identity in wanted
                    if identity.split(":", 1)[0] == point["identity"].split(":", 1)[1]
                ]
            for identity in targets:
                key = (identity, point["dimension"])
                grouped.setdefault(key, []).append(point["evidence"])
    result = {}
    for (identity, dimension), points in grouped.items():
        points.sort(key=lambda p: p["observed_at"])
        latest = points[-1]
        if (
            now is not None
            and not 0
            <= (aware_utc(now) - datetime.fromisoformat(latest["observed_at"])).total_seconds()
            <= 86400
        ):
            continue
        same_time = [p for p in points if p["observed_at"] == latest["observed_at"]]
        semantic_values = {
            (p["value"], p["metric"], p.get("sample_count"), p.get("period_seconds"))
            for p in same_time
        }
        if len(semantic_values) != 1:
            result.setdefault(identity, {})[dimension] = {
                "conflict": "CONTRADICTORY_DOCUMENTED_OBSERVATIONS"
            }
            continue
        result.setdefault(identity, {})[dimension] = latest
        if dimension == "popularity" and latest["metric"] == "LIFETIME_SALES":
            conflicting = any(
                len(
                    {
                        (v["value"], v["metric"])
                        for v in points
                        if v["observed_at"] == p["observed_at"]
                    }
                )
                > 1
                for p in points
            )
            if conflicting:
                result[identity]["trend"] = []
                continue
            unique = {p["observed_at"]: p for p in points if p["metric"] == latest["metric"]}
            result[identity]["trend"] = list(unique.values())[-100:]
    return result

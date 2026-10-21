"""Local Telegram destination configuration; no Telegram API calls or secrets."""

import argparse
import json
import re

from sqlalchemy import select

from app.integrations.errors import IntegrationError
from app.models import (
    Category,
    CategoryMode,
    DestinationCategory,
    DestinationChannel,
    PublicationDestination,
    TelegramPublication,
    TelegramPublicationStatus,
)

TEST_GROUP_NAME = "Viana Ofertas"
TEST_GROUP_CHAT_ID = "-5309308689"


def validate_chat_id(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"-[1-9][0-9]{0,15}", value)
        or abs(int(value)) > 2**52 - 1
    ):
        raise ValueError("Use o identificador numérico negativo de um grupo Telegram.")
    return value


def invalidate_reviews(session, destination_id):
    """Configuration edits never release SENDING/UNCERTAIN or erase sent history."""
    for entry in session.scalars(
        select(TelegramPublication)
        .where(
            TelegramPublication.destination_id == destination_id,
            TelegramPublication.status.in_(
                (TelegramPublicationStatus.PENDING, TelegramPublicationStatus.APPROVED)
            ),
        )
        .with_for_update()
    ):
        entry.status = TelegramPublicationStatus.EXPIRED
        entry.active_slot = None
        entry.approved_by = entry.approved_at = None
        entry.last_error_code = "DESTINATION_CHANGED"
    session.flush()


def destination_or_error(session, destination_id):
    destination = session.scalar(
        select(PublicationDestination)
        .where(
            PublicationDestination.id == destination_id,
            PublicationDestination.channel == DestinationChannel.TELEGRAM,
        )
        .with_for_update()
    )
    if destination is None:
        raise IntegrationError("DESTINATION_NOT_FOUND", "Destino Telegram não encontrado.")
    return destination


def configure_destination(
    session,
    *,
    destination=None,
    name=None,
    external_id=None,
    enabled=None,
    manual_approval_required=True,
    category_mode=None,
    category_ids=None,
    min_interval_seconds=None,
    max_messages_per_hour=None,
):
    if destination is not None and destination.channel != DestinationChannel.TELEGRAM:
        raise ValueError("A configuração exige um destino Telegram.")
    if manual_approval_required is not True:
        raise ValueError("A aprovação manual é obrigatória nos destinos Telegram.")
    if name is not None and (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 200
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise ValueError("Nome de destino inválido.")
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("Estado de pausa inválido.")
    for value, maximum in ((min_interval_seconds, 86400), (max_messages_per_hour, 1000)):
        if value is not None and (type(value) is not int or not 1 <= value <= maximum):
            raise ValueError("Limite de envio inválido.")
    if category_mode is not None:
        category_mode = CategoryMode(category_mode)
    if category_ids is not None:
        if (
            any(type(value) is not int or value <= 0 for value in category_ids)
            or len(category_ids) > 100
        ):
            raise ValueError("Seleção de categorias inválida.")
        ids = set(category_ids)
        if len(ids) != len(category_ids):
            raise ValueError("Não repita categorias na seleção.")
        if ids != set(session.scalars(select(Category.id).where(Category.id.in_(ids)))):
            raise ValueError("Uma ou mais categorias não existem.")
    if destination is None:
        if name is None or external_id is None:
            raise ValueError("Nome e identificador do grupo são obrigatórios.")
        validate_chat_id(external_id)
        destination = PublicationDestination(
            name=name.strip(),
            external_id=external_id,
            channel=DestinationChannel.TELEGRAM,
            enabled=False,
            manual_approval_required=True,
            category_mode=CategoryMode.UNCONFIGURED,
        )
        session.add(destination)
        session.flush()
    elif external_id is not None and external_id != destination.external_id:
        raise ValueError("O identificador do grupo é imutável; cadastre outro destino.")
    effective_mode = category_mode if category_mode is not None else destination.category_mode
    if category_ids and effective_mode != CategoryMode.SELECTED:
        raise ValueError("Somente o modo selected aceita uma lista de categorias.")
    if category_mode is not None and effective_mode != CategoryMode.SELECTED:
        category_ids = []
    changes = {"manual_approval_required": True}
    for key, value in (
        ("name", name.strip() if name else None),
        ("enabled", enabled),
        ("category_mode", category_mode),
        ("min_interval_seconds", min_interval_seconds),
        ("max_messages_per_hour", max_messages_per_hour),
    ):
        if value is not None:
            changes[key] = value
    changed = any(getattr(destination, key) != value for key, value in changes.items())
    for key, value in changes.items():
        setattr(destination, key, value)
    if category_ids is not None:
        current = {
            row.category_id: row
            for row in session.scalars(
                select(DestinationCategory).where(
                    DestinationCategory.destination_id == destination.id
                )
            )
        }
        desired = set(category_ids)
        changed |= set(current) != desired
        for key in set(current) - desired:
            session.delete(current[key])
        for key in desired - set(current):
            session.add(DestinationCategory(destination_id=destination.id, category_id=key))
    if changed:
        invalidate_reviews(session, destination.id)
    session.flush()
    return destination


def prepare_test_group(session):
    destination = session.scalar(
        select(PublicationDestination)
        .where(
            PublicationDestination.channel == DestinationChannel.TELEGRAM,
            PublicationDestination.external_id == TEST_GROUP_CHAT_ID,
        )
        .with_for_update()
    )
    return configure_destination(
        session,
        destination=destination,
        name=TEST_GROUP_NAME,
        external_id=TEST_GROUP_CHAT_ID,
        enabled=False,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Prepara o grupo informado, sempre pausado, sem rede Telegram."
    )
    parser.add_argument("command", choices=["prepare-test-group"])
    parser.parse_args()
    from app.core.config import Settings
    from app.db import build_engine, build_session_factory

    engine = None
    try:
        settings = Settings()
        if settings.telegram_publisher_enabled or settings.telegram_real_sends_authorized:
            raise ValueError("Mantenha ambas as flags Telegram desativadas.")
        engine = build_engine(settings.database_url)
        with build_session_factory(engine)() as session:
            destination = prepare_test_group(session)
            session.commit()
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "id": destination.id,
                        "name": destination.name,
                        "chat_id": destination.external_id,
                        "enabled": destination.enabled,
                        "manual_approval_required": destination.manual_approval_required,
                    }
                )
            )
        return 0
    except Exception as error:
        print(json.dumps({"status": "pending", "error_type": type(error).__name__}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

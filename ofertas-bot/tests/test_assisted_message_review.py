"""A paused group can review the exact message without granting dispatch permissions."""

import pytest
import test_manual_offers as manual
from sqlalchemy import func, select

from app.integrations.errors import IntegrationError
from app.models import Offer, ProductPriceHistory, PublicationDestination, TelegramPublication
from app.services.telegram_publication import TelegramPublicationService

context = manual.context
no_external_calls = manual.no_external_calls


def test_paused_review_matches_actual_preview_after_authorized_activation(context):
    factory, settings, _, destination_id = context
    offer_id = manual.register(context)
    service = TelegramPublicationService(settings, clock=lambda: manual.NOW)
    with factory() as session:
        session.get(PublicationDestination, destination_id).enabled = False
        session.commit()
    with factory() as session:
        result = service.review_message(session, offer_id, destination_id, include_image=False)
        assert not result["destination_enabled"] and not result["can_approve"]
        assert not result["publication_authorized"] and not result["outbox_created"]
        assert result["hash_semantics"] == "CONTENT_REVIEW_ONLY_NOT_OUTBOX_APPROVAL_HASH"
        assert session.scalar(select(func.count()).select_from(TelegramPublication)) == 0
        assert session.scalar(select(func.count()).select_from(Offer)) == 1
        assert session.scalar(select(func.count()).select_from(ProductPriceHistory)) == 1
        assert not session.get(PublicationDestination, destination_id).enabled
    with pytest.raises(IntegrationError) as disabled:
        service.publish(factory, 999)
    assert disabled.value.code == "PUBLISHER_DISABLED"
    # In the isolated test only, authorize activation and prove that no alternative
    # renderer or content is substituted into the normal publication flow.
    with factory() as session:
        session.get(PublicationDestination, destination_id).enabled = True
        session.commit()
    with factory() as session:
        entry = service.preview(session, offer_id, destination_id, include_image=False)
        assert entry.preview_payload["message"] == result["content"]["message"]
        assert entry.preview_payload["chat_id"] == result["content"]["chat_id"]
        assert entry.approved_by is None


def test_readonly_review_still_requires_current_verified_offer(context):
    factory, settings, _, destination_id = context
    service = TelegramPublicationService(settings, clock=lambda: manual.NOW)
    with factory() as session:
        with pytest.raises(IntegrationError) as missing:
            service.review_message(session, 999, destination_id)
        assert missing.value.code == "MANUAL_OFFER_NOT_FOUND"
        assert session.scalar(select(func.count()).select_from(TelegramPublication)) == 0

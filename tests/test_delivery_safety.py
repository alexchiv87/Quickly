"""Delivery identity, reconciliation, and global suppression safety gates."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.campaign_lead_status import (
    campaign_lead_may_receive_sends,
    campaign_lead_schedule_eligibility_clause,
)
from app.delivery_safety import deterministic_message_id, reconcile_smtp_copy
from app.models import Campaign, CampaignLead, Inbox, Lead, QueueSlot, SmtpAccount
from app.routers.leads import suppress_lead
from app.schemas import LeadSuppressionRequest
from tests.conftest import (
    make_campaign,
    make_campaign_inbox,
    make_campaign_lead,
    make_inbox,
    make_lead,
    make_queue_slot,
)


def _reconciliation_account() -> SmtpAccount:
    return SmtpAccount(
        inbox_id=1,
        smtp_host="mail.example.com",
        smtp_username="sender@example.com",
        smtp_password="secret",
        imap_host="mail.example.com",
        imap_port=993,
        imap_username="sender@example.com",
        imap_password="secret",
        imap_use_ssl=True,
        reconciliation_copy_enabled=True,
    )


def test_deterministic_message_id_is_stable_and_opaque() -> None:
    first = deterministic_message_id(
        queue_slot_id=12, inbox_id=3, campaign_id=9, from_email="sender@EXAMPLE.com"
    )
    second = deterministic_message_id(
        queue_slot_id=12, inbox_id=3, campaign_id=9, from_email="sender@example.com"
    )
    assert first == second
    assert first.endswith("@example.com>")
    assert "12:3:9" not in first


def test_reconciliation_requires_exactly_one_read_only_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeImap:
        def __init__(self, host: str, port: int) -> None:
            calls["connect"] = (host, port)

        def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
            calls["login"] = (username, password)
            return "OK", []

        def select(
            self, mailbox: str, readonly: bool = False
        ) -> tuple[str, list[bytes]]:
            calls["select"] = (mailbox, readonly)
            return "OK", [b"1"]

        def search(
            self, charset: object, *criteria: str
        ) -> tuple[str, list[bytes]]:
            calls["search"] = (charset, criteria)
            return "OK", [b"42"]

        def close(self) -> tuple[str, list[bytes]]:
            return "OK", []

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", []

    monkeypatch.setattr("app.delivery_safety.imaplib.IMAP4_SSL", FakeImap)
    result = reconcile_smtp_copy(_reconciliation_account(), "<stable@example.com>")

    assert result.outcome == "confirmed"
    assert calls["select"] == ("INBOX", True)
    assert calls["search"] == (
        None,
        ("HEADER", "Message-ID", '"<stable@example.com>"'),
    )


@pytest.mark.asyncio
async def test_complaint_globally_suppresses_and_pauses_senders(
    session: AsyncSession,
) -> None:
    lead = await make_lead(session)
    campaign_a = await make_campaign(session, name="A")
    campaign_b = await make_campaign(session, name="B")
    inbox_a = await make_inbox(session, email="a@example.com")
    inbox_b = await make_inbox(session, email="b@example.com")
    enrollment_a = await make_campaign_lead(session, campaign_a.id, lead.id)
    enrollment_b = await make_campaign_lead(session, campaign_b.id, lead.id)
    await make_campaign_inbox(session, campaign_a.id, inbox_a.id)
    await make_campaign_inbox(session, campaign_b.id, inbox_b.id)
    await make_queue_slot(session, enrollment_a.id, inbox_a.id)
    await make_queue_slot(session, enrollment_b.id, inbox_b.id)

    response = await suppress_lead(
        lead.id,
        LeadSuppressionRequest(reason="complaint"),
        db=session,
    )

    assert response.status == "complaint"
    assert response.enrollments_stopped == 2
    assert response.queue_slots_deleted == 2
    assert response.campaigns_paused == 2
    assert response.inboxes_paused == 2
    assert (await session.execute(select(func.count(QueueSlot.id)))).scalar_one() == 0
    assert all(
        row.paused for row in (await session.execute(select(Campaign))).scalars().all()
    )
    assert all(
        row.paused for row in (await session.execute(select(Inbox))).scalars().all()
    )
    assert all(
        row.sending_paused
        for row in (await session.execute(select(CampaignLead))).scalars().all()
    )
    assert campaign_lead_may_receive_sends(enrollment_a, lead) is False
    eligible = await session.execute(
        select(CampaignLead.id)
        .join(Lead, Lead.id == CampaignLead.lead_id)
        .join(Campaign, Campaign.id == CampaignLead.campaign_id)
        .where(campaign_lead_schedule_eligibility_clause())
    )
    assert eligible.scalars().all() == []

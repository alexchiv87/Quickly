"""Global lead suppression shared by public and operator-driven stop paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, CampaignLead, Inbox, Lead, QueueSlot

SuppressionReason = Literal["unsubscribed", "complaint", "hard_bounce", "manual"]


@dataclass(frozen=True)
class SuppressionResult:
    lead_id: int
    status: str
    enrollments_stopped: int
    queue_slots_deleted: int
    campaigns_paused: int = 0
    inboxes_paused: int = 0


async def suppress_lead_globally(
    db: AsyncSession,
    lead: Lead,
    reason: SuppressionReason,
) -> SuppressionResult:
    """Stop a lead across every campaign; complaints also stop all senders."""
    lead_status = {
        "unsubscribed": "unsubscribed",
        "complaint": "complaint",
        "hard_bounce": "bounced",
        "manual": "suppressed",
    }[reason]
    enrollment_status = "bounced" if reason == "hard_bounce" else "unsubscribed"

    enrollments_result = await db.execute(
        select(CampaignLead).where(CampaignLead.lead_id == lead.id)
    )
    enrollments = list(enrollments_result.scalars().all())
    enrollment_ids = [enrollment.id for enrollment in enrollments]
    queue_slots_deleted = 0
    if enrollment_ids:
        count_result = await db.execute(
            select(func.count(QueueSlot.id)).where(
                QueueSlot.campaign_lead_id.in_(enrollment_ids)
            )
        )
        queue_slots_deleted = int(count_result.scalar_one())
        await db.execute(
            delete(QueueSlot).where(QueueSlot.campaign_lead_id.in_(enrollment_ids))
        )

    for enrollment in enrollments:
        enrollment.enrollment_status = enrollment_status
        enrollment.interest_status = None
        enrollment.sending_paused = True
    lead.status = lead_status

    campaigns_paused = 0
    inboxes_paused = 0
    if reason == "complaint":
        campaigns_result = await db.execute(
            select(func.count(Campaign.id)).where(Campaign.paused.is_(False))
        )
        campaigns_paused = int(campaigns_result.scalar_one())
        inboxes_result = await db.execute(
            select(func.count(Inbox.id)).where(Inbox.paused.is_(False))
        )
        inboxes_paused = int(inboxes_result.scalar_one())
        await db.execute(
            update(Campaign).where(Campaign.paused.is_(False)).values(paused=True)
        )
        await db.execute(
            update(Inbox).where(Inbox.paused.is_(False)).values(paused=True)
        )

    return SuppressionResult(
        lead_id=lead.id,
        status=lead_status,
        enrollments_stopped=len(enrollments),
        queue_slots_deleted=queue_slots_deleted,
        campaigns_paused=campaigns_paused,
        inboxes_paused=inboxes_paused,
    )

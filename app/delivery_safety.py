"""Durable SMTP identity and read-only reconciliation helpers."""

from __future__ import annotations

import hashlib
import imaplib
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.models import SmtpAccount

ReconciliationOutcome = Literal["confirmed", "not_found", "duplicate", "error"]


@dataclass(frozen=True)
class ReconciliationResult:
    outcome: ReconciliationOutcome
    detail: str = ""


class ImapSearchResponse(BaseModel):
    """Parsed IMAP SEARCH response at the network boundary."""

    status: Literal["OK", "NO", "BAD"]
    message_numbers: tuple[bytes, ...]


def deterministic_message_id(
    *, queue_slot_id: int, inbox_id: int, campaign_id: int, from_email: str
) -> str:
    """Return a stable, opaque RFC Message-ID for one immutable queue slot."""
    raw_domain = from_email.rpartition("@")[2].strip().lower()
    domain = re.sub(r"[^a-z0-9.-]", "", raw_domain) or "quickly.local"
    identity = f"{queue_slot_id}:{inbox_id}:{campaign_id}".encode("ascii")
    digest = hashlib.sha256(identity).hexdigest()[:32]
    return f"<quickly-{digest}@{domain}>"


def _parse_search_response(
    status: str, payload: list[bytes | None]
) -> ImapSearchResponse:
    first = payload[0] if payload else b""
    return ImapSearchResponse.model_validate(
        {
            "status": status.upper(),
            "message_numbers": tuple((first or b"").split()),
        }
    )


def reconcile_smtp_copy(account: SmtpAccount, message_id: str) -> ReconciliationResult:
    """Look for exactly one envelope-only reconciliation copy in IMAP INBOX.

    The mailbox is opened read-only. A missing or duplicate match is never
    treated as proof that retrying the original recipient is safe.
    """
    if not account.reconciliation_copy_enabled:
        return ReconciliationResult("error", "reconciliation copy is disabled")
    if not (account.imap_host or "").strip():
        return ReconciliationResult("error", "IMAP host is not configured")
    if not (account.imap_username or "").strip() or not account.imap_password:
        return ReconciliationResult("error", "IMAP credentials are not configured")

    client: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
    selected = False
    try:
        client_type = imaplib.IMAP4_SSL if account.imap_use_ssl else imaplib.IMAP4
        client = client_type(account.imap_host, account.imap_port)
        client.login(account.imap_username, account.imap_password)
        status, _ = client.select("INBOX", readonly=True)
        if status.upper() != "OK":
            return ReconciliationResult("error", f"IMAP select returned {status}")
        selected = True
        status, payload = client.search(None, "HEADER", "Message-ID", f'"{message_id}"')
        parsed = _parse_search_response(status, payload)
        if parsed.status != "OK":
            return ReconciliationResult(
                "error", f"IMAP search returned {parsed.status}"
            )
        if len(parsed.message_numbers) == 1:
            return ReconciliationResult("confirmed")
        if not parsed.message_numbers:
            return ReconciliationResult("not_found")
        return ReconciliationResult(
            "duplicate",
            f"found {len(parsed.message_numbers)} copies with the same Message-ID",
        )
    except (imaplib.IMAP4.error, OSError, ValidationError) as exc:
        return ReconciliationResult("error", str(exc)[:300])
    finally:
        if client is not None:
            if selected:
                with suppress(imaplib.IMAP4.error, OSError):
                    client.close()
            with suppress(imaplib.IMAP4.error, OSError):
                client.logout()

# Delivery safety for self-hosted SMTP

Quickly's **ramp-up** controls daily volume. It does not create mailbox
engagement, repair domain reputation, or guarantee inbox placement. Treat it
as a rate limiter and monitor real bounces, complaints, replies, and provider
postmaster data separately.

## Preventing duplicate SMTP delivery

Each SMTP queue slot receives one durable attempt and one deterministic
`Message-ID`. Quickly commits that identity before contacting the relay. If
the worker loses the connection after SMTP `DATA`, the outcome becomes
`ambiguous`, the queue slot is held, and the inbox is paused. Quickly does not
retry an ambiguous delivery automatically.

For a self-hosted mailbox such as Stalwart, enable an envelope-only copy to the
sending mailbox:

```json
{
  "smtp_host": "mail.example.com",
  "smtp_port": 587,
  "smtp_username": "sender@example.com",
  "smtp_password": "...",
  "smtp_use_tls": true,
  "smtp_use_ssl": false,
  "imap_host": "mail.example.com",
  "imap_port": 993,
  "imap_username": "sender@example.com",
  "imap_password": "...",
  "imap_use_ssl": true,
  "reconciliation_copy_enabled": true
}
```

Send this body to `PUT /api/smtp/inboxes/{id}`. The copy is added only to the
SMTP envelope, so the lead does not see the sender as a second recipient. On
the next slot scan, Quickly opens `INBOX` read-only and searches for the exact
`Message-ID`:

- one match confirms delivery and closes the slot without another send;
- zero or multiple matches keep the slot held;
- an IMAP error keeps the slot held.

The inbox remains paused after reconciliation. Inspect the relay and IMAP
state before resuming it.

Do not enable this option for send-only relays without a mailbox, such as a
plain Amazon SES SMTP endpoint. An ambiguous delivery on those relays requires
manual provider-log reconciliation.

## Global suppression

The public unsubscribe link now suppresses the lead across every campaign and
deletes all pending slots for that lead. Scheduling and send-time checks both
honour the lead's global status.

Operators can apply an explicit stop with:

```http
POST /api/leads/{id}/suppress
Content-Type: application/json

{"reason":"unsubscribed"}
```

Supported reasons are `unsubscribed`, `hard_bounce`, `manual`, and
`complaint`. A complaint is a full safety stop: it suppresses the lead and
pauses every campaign and inbox until an operator reviews the incident.

## Recommended launch sequence

1. Configure SPF, DKIM, DMARC reporting, PTR/rDNS, and TLS for the sending
   domain and mail host.
2. Use separate outreach mailboxes and keep the primary product domain out of
   cold campaigns.
3. Enable reconciliation copies for self-hosted SMTP mailboxes and verify IMAP
   search before importing leads.
4. Verify addresses before enrollment, start with a low daily limit, and raise
   it only while bounce and complaint signals remain healthy.
5. Keep open and click tracking off unless a campaign needs it. Replies and
   qualified conversions are more reliable offer-test signals.
6. Test unsubscribe and complaint suppression with queued leads before the
   first live campaign.

"""Notifications: the few things people must act on, sent to email and a chat webhook (FR-024).

Off by default: nothing leaves Baluarte until an administrator configures a channel under
Settings → Notifications. See docs/NOTIFICATIONS.md.

- ``settings``: the channels (SMTP, one HTTPS webhook) and which events go where; secrets are
  sealed in ``app_secret`` and never returned.
- ``channels``: the senders. TLS is always verified; nothing is sent in plain text.
- ``messages``: the message texts. No personal data: counts, project paths, rule IDs and links.
- ``events``: what produces a notification, each at most once by its ``dedup_key``.
- ``delivery``: the ``notify.deliver`` job, its schedule and retries.
"""

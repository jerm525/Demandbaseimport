"""
Pluggable alert notifier. The actual channel/recipient is not yet decided
(see README "Flagged Items"), so this defaults to a no-op that just logs --
swap NotifierConfig.channel to "email" or "webhook" once that's confirmed,
and fill in EmailNotifier/WebhookNotifier's real send logic.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol

from .config import AppConfig

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def alert(self, subject: str, body: str) -> None: ...


class NoOpNotifier:
    """Default notifier: logs at ERROR level only. Safe placeholder until a
    real channel is confirmed."""

    def alert(self, subject: str, body: str) -> None:
        logger.error("ALERT (no-op notifier, no channel configured): %s | %s", subject, body)


class WebhookNotifier:
    """Stub -- wire in the real webhook call once a URL is confirmed."""

    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url

    def alert(self, subject: str, body: str) -> None:
        import requests

        try:
            requests.post(
                self._webhook_url,
                json={"subject": subject, "body": body},
                timeout=10,
            )
        except requests.RequestException:
            logger.exception("Failed to deliver webhook alert: %s", subject)


class EmailNotifier:
    """Stub -- wire in real SMTP/mail-API sending once channel/recipient is
    confirmed. Currently only logs, so nothing silently disappears."""

    def __init__(self, recipient: str):
        self._recipient = recipient

    def alert(self, subject: str, body: str) -> None:
        logger.error(
            "ALERT (email notifier stub -- not actually sending email yet) "
            "to=%s subject=%s body=%s",
            self._recipient,
            subject,
            body,
        )


def build_notifier(config: AppConfig) -> Notifier:
    channel = config.notifier.channel
    if channel == "webhook":
        url = os.environ.get(config.notifier.webhook_url_env_var or "")
        if not url:
            logger.warning("Webhook channel configured but URL env var not set; falling back to no-op.")
            return NoOpNotifier()
        return WebhookNotifier(url)
    if channel == "email":
        recipient = os.environ.get(config.notifier.email_to_env_var or "")
        if not recipient:
            logger.warning("Email channel configured but recipient env var not set; falling back to no-op.")
            return NoOpNotifier()
        return EmailNotifier(recipient)
    return NoOpNotifier()

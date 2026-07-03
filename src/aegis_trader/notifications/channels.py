"""Notification channels.

Each channel implements ``send`` and reports failures without raising —
a broken webhook must never take down the trading loop. Credentials come
from the vault; channel-specific settings from config ``options``.
"""

from __future__ import annotations

import abc
import asyncio
import json
import shutil
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx
import structlog

from ..core.enums import NotificationLevel

log = structlog.get_logger(__name__)

_LEVEL_ORDER = {NotificationLevel.INFO: 0, NotificationLevel.WARNING: 1, NotificationLevel.CRITICAL: 2}


class Channel(abc.ABC):
    kind: str = ""

    def __init__(self, *, credential: dict[str, str] | None,
                 options: dict[str, Any], min_level: NotificationLevel) -> None:
        self._credential = credential or {}
        self._options = options
        self._min_level = min_level

    def accepts(self, level: NotificationLevel) -> bool:
        return _LEVEL_ORDER[level] >= _LEVEL_ORDER[self._min_level]

    @abc.abstractmethod
    async def send(self, level: NotificationLevel, title: str, body: str) -> bool: ...


class DesktopChannel(Channel):
    """Linux desktop notifications via notify-send (libnotify)."""

    kind = "desktop"

    async def send(self, level: NotificationLevel, title: str, body: str) -> bool:
        binary = shutil.which("notify-send")
        if binary is None:
            log.warning("notify-send not found; desktop notifications disabled")
            return False
        urgency = {"info": "normal", "warning": "normal", "critical": "critical"}[level.value]
        process = await asyncio.create_subprocess_exec(
            binary, "--app-name=Aegis Trader", f"--urgency={urgency}",
            f"Aegis: {title}", body[:1000],
        )
        await process.wait()
        return process.returncode == 0


class EmailChannel(Channel):
    """SMTP email. Credential JSON: {"password": "..."}; options: host, port,
    username, from_addr, to_addr, starttls (default true)."""

    kind = "email"

    async def send(self, level: NotificationLevel, title: str, body: str) -> bool:
        return await asyncio.to_thread(self._send_sync, level, title, body)

    def _send_sync(self, level: NotificationLevel, title: str, body: str) -> bool:
        try:
            message = EmailMessage()
            message["Subject"] = f"[Aegis {level.value.upper()}] {title}"
            message["From"] = self._options["from_addr"]
            message["To"] = self._options["to_addr"]
            message.set_content(body)
            host = self._options["host"]
            port = int(self._options.get("port", 587))
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if self._options.get("starttls", True):
                    smtp.starttls()
                username = self._options.get("username")
                password = self._credential.get("password")
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
            return True
        except Exception as exc:
            log.warning("email notification failed", error=str(exc))
            return False


class _HttpChannel(Channel):
    async def _post(self, url: str, payload: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json=payload)
            if response.status_code >= 400:
                log.warning(f"{self.kind} notification failed",
                            status=response.status_code, body=response.text[:200])
                return False
            return True
        except httpx.HTTPError as exc:
            log.warning(f"{self.kind} notification failed", error=str(exc))
            return False


class DiscordChannel(_HttpChannel):
    """Discord webhook. Credential JSON: {"webhook_url": "..."}."""

    kind = "discord"

    async def send(self, level: NotificationLevel, title: str, body: str) -> bool:
        url = self._credential.get("webhook_url", "")
        if not url:
            return False
        color = {"info": 0x2ECC71, "warning": 0xF1C40F, "critical": 0xE74C3C}[level.value]
        return await self._post(url, {
            "embeds": [{"title": f"Aegis — {title}", "description": body[:3900], "color": color}]
        })


class TelegramChannel(_HttpChannel):
    """Telegram bot. Credential JSON: {"bot_token": "...", "chat_id": "..."}."""

    kind = "telegram"

    async def send(self, level: NotificationLevel, title: str, body: str) -> bool:
        token = self._credential.get("bot_token", "")
        chat_id = self._credential.get("chat_id", "")
        if not token or not chat_id:
            return False
        prefix = {"info": "i", "warning": "!", "critical": "!!"}[level.value]
        text = f"[{prefix}] Aegis — {title}\n\n{body[:3800]}"
        return await self._post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": text},
        )


class WebhookChannel(_HttpChannel):
    """Generic JSON webhook (also covers push services like ntfy/gotify via
    their JSON endpoints). Credential JSON: {"url": "...", "token": "..."?}."""

    kind = "webhook"

    async def send(self, level: NotificationLevel, title: str, body: str) -> bool:
        url = self._credential.get("url") or self._options.get("url", "")
        if not url:
            return False
        payload = {"source": "aegis-trader", "level": level.value, "title": title, "body": body}
        extra = self._options.get("extra_fields")
        if isinstance(extra, dict):
            payload.update(extra)
        token = self._credential.get("token")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    url, content=json.dumps(payload),
                    headers={"Content-Type": "application/json",
                             **({"Authorization": f"Bearer {token}"} if token else {})},
                )
            return response.status_code < 400
        except httpx.HTTPError as exc:
            log.warning("webhook notification failed", error=str(exc))
            return False


CHANNEL_KINDS: dict[str, type[Channel]] = {
    c.kind: c for c in (DesktopChannel, EmailChannel, DiscordChannel, TelegramChannel, WebhookChannel)
}

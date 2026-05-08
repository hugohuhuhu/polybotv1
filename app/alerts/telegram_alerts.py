from __future__ import annotations

import html

import httpx

from app.models.core import Opportunity


class TelegramAlerts:
    """Minimal Telegram notifier."""

    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, opportunity: Opportunity) -> bool:
        if not self.enabled:
            return False
        return await self.send_text(self._format_message(opportunity))

    async def send_text(self, message: str) -> bool:
        if not self.enabled:
            return False
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
        return True

    def _format_message(self, opportunity: Opportunity) -> str:
        slugs = "\n".join(
            f"https://polymarket.com/event/{html.escape(slug)}" for slug in opportunity.link_slugs
        )
        return (
            f"<b>Polymarket 掃描警示</b>\n"
            f"<b>策略：</b>{html.escape(opportunity.strategy_type.value)}\n"
            f"<b>標題：</b>{html.escape(opportunity.title)}\n"
            f"<b>淨邊際：</b>{opportunity.net_edge:.3%}\n"
            f"<b>流動性：</b>{opportunity.available_liquidity:,.0f}\n"
            f"<b>建議：</b>{html.escape(opportunity.suggested_action)}\n"
            f"<b>市場連結：</b>\n{slugs}"
        )

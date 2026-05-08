from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings


@dataclass(slots=True)
class TradingControls:
    live_trading_enabled: bool
    auto_execute_enabled: bool
    kill_switch_enabled: bool

    @property
    def armed(self) -> bool:
        return (
            self.live_trading_enabled
            and self.auto_execute_enabled
            and not self.kill_switch_enabled
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "live_trading_enabled": self.live_trading_enabled,
            "auto_execute_enabled": self.auto_execute_enabled,
            "kill_switch_enabled": self.kill_switch_enabled,
            "armed": self.armed,
        }

    def apply(self, settings: Settings) -> Settings:
        return settings.model_copy(
            update={
                "enable_live_trading": self.live_trading_enabled,
                "live_auto_execute": self.auto_execute_enabled,
                "risk_kill_switch": self.kill_switch_enabled,
            }
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "TradingControls":
        return cls(
            live_trading_enabled=settings.enable_live_trading,
            auto_execute_enabled=settings.live_auto_execute,
            kill_switch_enabled=settings.risk_kill_switch,
        )

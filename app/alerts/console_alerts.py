from __future__ import annotations

from rich.console import Console
from rich.table import Table

from app.models.core import EventRecord, MarketRecord, Opportunity


class ConsoleAlerts:
    """Rich-powered console output in Traditional Chinese."""

    def __init__(self) -> None:
        self.console = Console()

    def show_discovery_summary(self, events: list[EventRecord], markets: list[MarketRecord]) -> None:
        table = Table(title="市場探索摘要")
        table.add_column("指標")
        table.add_column("數值", justify="right")
        table.add_row("事件數", str(len(events)))
        table.add_row("市場數", str(len(markets)))
        binary_count = sum(1 for market in markets if market.is_binary)
        table.add_row("二元市場", str(binary_count))
        self.console.print(table)

    def show_markets(self, markets: list[MarketRecord], limit: int = 10) -> None:
        table = Table(title="市場清單")
        table.add_column("問題")
        table.add_column("Slug")
        table.add_column("流動性", justify="right")
        table.add_column("價差", justify="right")
        for market in markets[:limit]:
            table.add_row(
                market.question[:50],
                market.slug,
                f"{(market.liquidity or 0):,.0f}",
                f"{(market.spread or 0):.3f}",
            )
        self.console.print(table)

    def show_opportunities(self, opportunities: list[Opportunity], limit: int = 20) -> None:
        table = Table(title="掃描結果")
        table.add_column("排名", justify="right")
        table.add_column("策略")
        table.add_column("市場")
        table.add_column("淨邊際", justify="right")
        table.add_column("流動性", justify="right")
        table.add_column("信心", justify="right")
        for index, opportunity in enumerate(opportunities[:limit], start=1):
            table.add_row(
                str(index),
                opportunity.strategy_type.value,
                " / ".join(opportunity.market_slugs),
                f"{opportunity.net_edge:.3%}",
                f"{opportunity.available_liquidity:,.0f}",
                f"{opportunity.confidence_score:.0%}",
            )
        self.console.print(table)

    def print_alert(self, opportunity: Opportunity) -> None:
        self.console.print(
            f"[bold yellow]警示[/bold yellow] {opportunity.title}\n"
            f"建議：{opportunity.suggested_action}\n"
            f"淨邊際：{opportunity.net_edge:.3%} | 最大安全量：{opportunity.max_safe_size:,.0f}\n"
            f"市場：{' / '.join(opportunity.link_slugs)}"
        )

    def print_message(self, message: str, *, style: str = "bold red") -> None:
        self.console.print(f"[{style}]{message}[/{style}]")

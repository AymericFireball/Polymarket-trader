#!/usr/bin/env python3
"""
Polymarket Trading Terminal — Textual + Rich Dashboard
========================================================
Live-updating TUI. Textual handles layout/keybindings; Rich renders panels.

Usage:
    python dashboard.py
    python run.py dashboard

Keybindings:
    r  — refresh all panels
    s  — run scan (background)
    f  — fetch market data (background)
    p  — run paper-trade session (background)
    q  — quit
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.widgets import Static, Header
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.binding import Binding
from textual import work

from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.console import Group
from rich import box


# ─── DB helper (opens fresh connection each call to avoid thread issues) ──────

def _db():
    from db import get_conn, init_db
    init_db()
    return get_conn()


# ─── Rendering helpers ────────────────────────────────────────────────────────

def _pnl(val: float) -> Text:
    sign = "+" if val > 0 else ""
    style = "bold green" if val > 0 else ("bold red" if val < 0 else "dim")
    return Text(f"{sign}${val:.2f}", style=style)


def _cut(s: str, n: int = 36) -> str:
    s = str(s or "")
    return s[: n - 1] + "…" if len(s) > n else s


def _age(ts: str, now: datetime) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        h = (now - dt).total_seconds() / 3600
        return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}d"
    except Exception:
        return "?"


# ─── Panel builders (pure Rich, no Textual) ──────────────────────────────────

def _build_portfolio() -> Panel:
    from config import BANKROLL, CASH_RESERVE_PCT

    conn = _db()
    try:
        live   = conn.execute(
            "SELECT * FROM trades WHERE status='open' AND (is_paper IS NULL OR is_paper=0)"
        ).fetchall()
        closed = conn.execute(
            "SELECT * FROM trades WHERE status!='open' AND (is_paper IS NULL OR is_paper=0)"
        ).fetchall()
    finally:
        conn.close()

    deployed   = sum(float(t["cost_basis"] or 0) for t in live)
    realized   = sum(float(t["realized_pnl"] or 0) for t in closed)
    unrealized = sum(float(t["unrealized_pnl"] or 0) for t in live)
    total_pnl  = realized + unrealized
    cash       = BANKROLL + realized - deployed
    cash_pct   = cash / BANKROLL if BANKROLL else 0
    now_bk     = BANKROLL + total_pnl

    filled  = int(min(now_bk / BANKROLL, 1.0) * 18)
    bar     = Text("█" * filled + "░" * (18 - filled), style="green")
    dd      = max(0.0, (BANKROLL - now_bk) / BANKROLL * 100)
    dd_col  = "green" if dd < 5 else ("yellow" if dd < 20 else "bold red")
    mode, mc = ("HALTED", "bold red") if dd >= 35 else \
               ("DEFENSIVE", "bold yellow") if dd >= 20 else \
               ("NORMAL", "bold green")

    g = Table.grid(padding=(0, 1))
    g.add_column(style="bold",    width=14)
    g.add_column(justify="right", width=10)
    g.add_column()

    g.add_row("Bankroll",  f"${now_bk:>8.2f}", bar)
    g.add_row("Cash",      f"${cash:>8.2f}",
              Text(f"({cash_pct:.0%}) ", style="dim") +
              Text("OK" if cash_pct >= CASH_RESERVE_PCT else "LOW",
                   style="green" if cash_pct >= CASH_RESERVE_PCT else "bold red"))
    g.add_row("Deployed",  f"${deployed:>8.2f}",
              Text(f"{len(live)} position(s)", style="dim"))
    g.add_row("", "", "")
    g.add_row("Realized",  "", _pnl(realized))
    g.add_row("Unrealized","", _pnl(unrealized))
    g.add_row("Total P&L", "", _pnl(total_pnl))
    g.add_row("", "", "")
    g.add_row("Drawdown",
              Text(f"{dd:.1f}%", style=dd_col),
              Text(mode, style=mc))

    return Panel(g, title="[bold cyan]PORTFOLIO[/]", border_style="cyan", box=box.ROUNDED)


def _build_paper_trades() -> Panel:
    conn = _db()
    try:
        open_t = conn.execute(
            "SELECT * FROM trades WHERE is_paper=1 AND status='open' ORDER BY opened_at DESC"
        ).fetchall()
        closed = conn.execute(
            "SELECT * FROM trades WHERE is_paper=1 AND status!='open'"
        ).fetchall()
    finally:
        conn.close()

    deployed   = sum(float(t["cost_basis"] or 0) for t in open_t)
    realized   = sum(float(t["realized_pnl"] or 0) for t in closed)
    unrealized = sum(float(t["unrealized_pnl"] or 0) for t in open_t)

    summary = Text()
    summary.append(f"{len(open_t)} open  {len(closed)} closed  ", style="dim")
    summary.append(f"deployed ${deployed:.2f}  ")
    summary.append("P&L ")
    summary.append_text(_pnl(realized + unrealized))

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold magenta", expand=True, show_edge=False)
    tbl.add_column("Market",  ratio=5, no_wrap=True)
    tbl.add_column("Side",    width=8)
    tbl.add_column("Entry",   justify="right", width=7)
    tbl.add_column("Now",     justify="right", width=7)
    tbl.add_column("P&L",     justify="right", width=9)

    if not open_t:
        tbl.add_row("[dim]No paper trades — run: python run.py paper-trade[/]",
                    "", "", "", "")
    else:
        for t in open_t:
            side  = t["side"] or "?"
            entry = float(t["entry_price"] or 0)
            cur   = float(t["current_price"] or entry)
            pnl   = float(t["unrealized_pnl"] or 0)
            tbl.add_row(
                _cut(t["question"] or t["condition_id"]),
                Text(side, style="green" if "YES" in side.upper() else "red"),
                f"${entry:.3f}", f"${cur:.3f}", _pnl(pnl),
            )

    return Panel(
        Group(summary, tbl),
        title=f"[bold cyan]PAPER TRADES[/] [dim]({len(open_t)} open, {len(closed)} closed)[/]",
        border_style="cyan",
        box=box.ROUNDED,
    )


def _build_signals() -> Panel:
    from config import NEWSAPI_KEY, MIROFISH_API_URL

    miro = False
    try:
        import urllib.request
        r = urllib.request.urlopen(f"{MIROFISH_API_URL}/health", timeout=1)
        miro = r.status == 200
    except Exception:
        pass

    rows = [
        ("Sharp Traders",  "7 whale wallets",          True),
        ("Base Rate",      "Historical calibration",    True),
        ("Cross-Platform", "Metaculus / Manifold",      True),
        ("News Sentiment", "NewsAPI",                   bool(NEWSAPI_KEY)),
        ("MiroFish Swarm", f"Docker @ {MIROFISH_API_URL}", miro),
    ]

    g = Table.grid(padding=(0, 1))
    g.add_column(width=10)
    g.add_column(style="bold", width=18)
    g.add_column(style="dim")

    for name, desc, up in rows:
        dot = Text("● ONLINE ", style="green") if up else Text("○ OFFLINE", style="red")
        g.add_row(dot, name, desc)

    conn = _db()
    try:
        n    = conn.execute(
            "SELECT COUNT(*) AS n FROM resolutions WHERE brier_score_ours IS NOT NULL"
        ).fetchone()["n"]
        br   = conn.execute(
            "SELECT AVG(brier_score_ours) as o, AVG(brier_score_market) as m "
            "FROM resolutions WHERE brier_score_ours IS NOT NULL"
        ).fetchone()
    finally:
        conn.close()

    cal = Text(f"\n  {n} resolved predictions", style="dim")
    if br and br["o"]:
        imp = ((br["m"] - br["o"]) / br["m"] * 100) if br["m"] else 0
        cal.append(f"  Brier {br['o']:.4f} vs mkt {br['m']:.4f}  ", style="dim")
        cal.append(f"{imp:+.1f}%", style="green" if imp > 0 else "red")

    return Panel(Group(g, cal), title="[bold cyan]SIGNALS[/]",
                 border_style="cyan", box=box.ROUNDED)


def _build_activity() -> Panel:
    now  = datetime.now(timezone.utc)
    conn = _db()
    try:
        preds = conn.execute(
            "SELECT question, our_estimate, market_price_at, delta, confidence, predicted_at "
            "FROM predictions ORDER BY predicted_at DESC LIMIT 8"
        ).fetchall()
    finally:
        conn.close()

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", expand=True, show_edge=False)
    tbl.add_column("",    width=2)
    tbl.add_column("Market", ratio=4, no_wrap=True)
    tbl.add_column("Est", justify="right", width=5)
    tbl.add_column("Mkt", justify="right", width=5)
    tbl.add_column("Δ",   justify="right", width=6)
    tbl.add_column("Conf",width=4)
    tbl.add_column("Age", justify="right", width=5, style="dim")

    if not preds:
        tbl.add_row("", "[dim]No predictions — run: python run.py scan --top 20[/]",
                    "", "", "", "", "")
    else:
        for p in preds:
            d  = float(p["delta"] or 0)
            c  = (p["confidence"] or "?").upper()
            arrow = "▲" if d > 0.02 else ("▼" if d < -0.02 else "─")
            dc    = "green" if d > 0.02 else ("red" if d < -0.02 else "dim")
            cc    = {"HIGH": "green", "MEDIUM": "yellow", "LOW": "red"}.get(c, "dim")
            tbl.add_row(
                Text(arrow, style=dc),
                _cut(p["question"] or "?", 32),
                f"{float(p['our_estimate'] or 0):.2f}",
                f"{float(p['market_price_at'] or 0):.2f}",
                Text(f"{d:+.2f}", style=dc),
                Text(c[:3], style=cc),
                _age(p["predicted_at"] or "", now),
            )

    return Panel(tbl, title="[bold cyan]RECENT ACTIVITY[/]",
                 border_style="cyan", box=box.ROUNDED)


def _build_market_overview() -> Panel:
    from db import db_stats
    conn = _db()
    try:
        stats = db_stats(conn)
    finally:
        conn.close()

    by_type = stats.get("by_type", {})
    icons   = {"political": "🗳", "crypto": "₿", "sports": "🏆",
               "geopolitical": "🌍", "regulatory": "📋", "other": "📊"}

    g = Table.grid(padding=(0, 1))
    g.add_column(width=4)
    g.add_column(width=16)
    g.add_column(width=26)
    g.add_column(justify="right", width=5)

    g.add_row("", Text("Active",   style="bold"),
              "", Text(str(stats.get("active_markets", 0)),   style="bold white"))
    g.add_row("", Text("Resolved", style="dim"),
              "", Text(str(stats.get("resolved_markets", 0)), style="dim"))
    g.add_row("", Text("Total",    style="dim"),
              "", Text(str(stats.get("markets", 0)),          style="dim"))

    if by_type:
        g.add_row("", "", "", "")
        mx = max(by_type.values()) if by_type else 1
        for cat, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
            bw = int(cnt / mx * 22) if mx else 0
            g.add_row(icons.get(cat, "  "),
                      (cat or "other").capitalize(),
                      Text("█" * bw, style="cyan"),
                      str(cnt))

    return Panel(g, title="[bold cyan]MARKETS[/]", border_style="cyan", box=box.ROUNDED)


# ─── Textual widgets (call builders, refresh on interval) ────────────────────

class PortfolioPanel(Static):
    DEFAULT_CSS = "PortfolioPanel { height: 1fr; border: blank; }"

    def render(self):
        try:
            return _build_portfolio()
        except Exception as e:
            return Panel(f"[red]Error: {e}[/]", title="PORTFOLIO", box=box.ROUNDED)

    def on_mount(self):
        self.set_interval(10, self.refresh)


class PaperTradesPanel(Static):
    DEFAULT_CSS = "PaperTradesPanel { height: 1fr; border: blank; }"

    def render(self):
        try:
            return _build_paper_trades()
        except Exception as e:
            return Panel(f"[red]Error: {e}[/]", title="PAPER TRADES", box=box.ROUNDED)

    def on_mount(self):
        self.set_interval(10, self.refresh)


class SignalsPanel(Static):
    DEFAULT_CSS = "SignalsPanel { height: 1fr; border: blank; }"

    def render(self):
        try:
            return _build_signals()
        except Exception as e:
            return Panel(f"[red]Error: {e}[/]", title="SIGNALS", box=box.ROUNDED)

    def on_mount(self):
        self.set_interval(30, self.refresh)


class ActivityPanel(Static):
    DEFAULT_CSS = "ActivityPanel { height: 1fr; border: blank; }"

    def render(self):
        try:
            return _build_activity()
        except Exception as e:
            return Panel(f"[red]Error: {e}[/]", title="ACTIVITY", box=box.ROUNDED)

    def on_mount(self):
        self.set_interval(10, self.refresh)


class MarketPanel(Static):
    DEFAULT_CSS = "MarketPanel { height: 14; border: blank; }"

    def render(self):
        try:
            return _build_market_overview()
        except Exception as e:
            return Panel(f"[red]Error: {e}[/]", title="MARKETS", box=box.ROUNDED)

    def on_mount(self):
        self.set_interval(60, self.refresh)


# ─── App ─────────────────────────────────────────────────────────────────────

class TradingApp(App):
    TITLE       = "Polymarket Trading Terminal"
    SUB_TITLE   = "v1.0 — Phase 5"

    CSS = """
    Screen {
        background: #0d1117;
    }
    #top {
        height: 1fr;
    }
    #left {
        width: 46;
        height: 100%;
    }
    #right {
        width: 1fr;
        height: 100%;
    }
    #bottom {
        height: 14;
    }
    """

    BINDINGS = [
        Binding("q", "quit",    "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "scan",    "Scan"),
        Binding("f", "fetch",   "Fetch"),
        Binding("p", "paper",   "Paper-trade"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            with Vertical(id="left"):
                yield PortfolioPanel()
                yield SignalsPanel()
            with Vertical(id="right"):
                yield PaperTradesPanel()
                yield ActivityPanel()
        with Horizontal(id="bottom"):
            yield MarketPanel()

    def action_refresh(self) -> None:
        for cls in (PortfolioPanel, PaperTradesPanel, SignalsPanel,
                    ActivityPanel, MarketPanel):
            for w in self.query(cls):
                w.refresh()
        self.notify("Refreshed", timeout=1.5)

    def action_scan(self) -> None:
        self.notify("Scanning markets…", timeout=3)
        self._bg(["python3", "run.py", "scan", "--top", "20"])

    def action_fetch(self) -> None:
        self.notify("Fetching market data…", timeout=3)
        self._bg(["python3", "run.py", "fetch"])

    def action_paper(self) -> None:
        self.notify("Running paper-trade session…", timeout=3)
        self._bg(["python3", "run.py", "paper-trade"])

    @work(thread=True)
    def _bg(self, cmd: list[str]) -> None:
        cwd = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(cmd, cwd=cwd, capture_output=True)
        self.call_from_thread(self.action_refresh)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    TradingApp().run()


if __name__ == "__main__":
    main()

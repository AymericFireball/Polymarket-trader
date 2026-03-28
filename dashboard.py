#!/usr/bin/env python3
"""
Polymarket Trading Terminal — Dashboard
=========================================
A slick terminal UI for monitoring your trading agent.

Usage:
    python dashboard.py              # Full dashboard
    python run.py dashboard          # Via main runner
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from db import get_conn, init_db, db_stats

# ─── ANSI Color Codes ────────────────────────────────────────────
class C:
    """Terminal colors and styles."""
    RESET      = "\033[0m"
    BOLD       = "\033[1m"
    DIM        = "\033[2m"
    UNDERLINE  = "\033[4m"
    BLINK      = "\033[5m"

    # Foreground
    BLACK      = "\033[30m"
    RED        = "\033[31m"
    GREEN      = "\033[32m"
    YELLOW     = "\033[33m"
    BLUE       = "\033[34m"
    MAGENTA    = "\033[35m"
    CYAN       = "\033[36m"
    WHITE      = "\033[37m"

    # Bright foreground
    BRED       = "\033[91m"
    BGREEN     = "\033[92m"
    BYELLOW    = "\033[93m"
    BBLUE      = "\033[94m"
    BMAGENTA   = "\033[95m"
    BCYAN      = "\033[96m"
    BWHITE     = "\033[97m"

    # Background
    BG_BLACK   = "\033[40m"
    BG_RED     = "\033[41m"
    BG_GREEN   = "\033[42m"
    BG_YELLOW  = "\033[43m"
    BG_BLUE    = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN    = "\033[46m"
    BG_WHITE   = "\033[47m"
    BG_GRAY    = "\033[100m"


def pnl_color(val):
    """Return green for positive, red for negative, dim for zero."""
    if val > 0:
        return C.BGREEN
    elif val < 0:
        return C.BRED
    return C.DIM


def pnl_str(val):
    """Format P&L with color and sign."""
    color = pnl_color(val)
    sign = "+" if val > 0 else ""
    return f"{color}{sign}${val:.2f}{C.RESET}"


def bar(pct, width=20, fill_char="█", empty_char="░"):
    """Create a progress bar."""
    filled = int(pct * width)
    return f"{C.BGREEN}{fill_char * filled}{C.DIM}{empty_char * (width - filled)}{C.RESET}"


def spark(values, width=12):
    """Create a sparkline from values."""
    if not values:
        return C.DIM + "─" * width + C.RESET
    blocks = " ▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    return "".join(blocks[min(8, int((v - mn) / rng * 8))] for v in values[-width:])


def truncate(s, length=40):
    """Truncate string with ellipsis."""
    s = str(s or "")
    return s[:length - 1] + "…" if len(s) > length else s


class Dashboard:
    """Terminal trading dashboard."""

    def __init__(self):
        init_db()
        self.conn = get_conn()
        self.now = datetime.now(timezone.utc)
        self.width = min(os.get_terminal_size().columns, 100) if sys.stdout.isatty() else 100

    def close(self):
        self.conn.close()

    def _line(self, char="─"):
        print(f"{C.DIM}{char * self.width}{C.RESET}")

    def _header(self, text):
        pad = self.width - len(text) - 4
        print(f"\n{C.BOLD}{C.BCYAN}┌─ {text} {'─' * max(pad, 0)}┐{C.RESET}")

    def _footer(self):
        print(f"{C.DIM}{'─' * self.width}{C.RESET}")

    def render(self):
        """Render the full dashboard."""
        self._render_banner()
        self._render_portfolio()
        self._render_open_trades()
        self._render_paper_trades()
        self._render_market_overview()
        self._render_signal_health()
        self._render_recent_activity()
        self._render_footer()

    # ─── Banner ──────────────────────────────────────────────────
    def _render_banner(self):
        os.system("clear" if os.name != "nt" else "cls")
        print()
        print(f"{C.BOLD}{C.BCYAN}  ╔══════════════════════════════════════════════════════════════╗{C.RESET}")
        print(f"{C.BOLD}{C.BCYAN}  ║{C.RESET}  {C.BOLD}{C.BWHITE}P O L Y M A R K E T   T R A D I N G   T E R M I N A L{C.RESET}   {C.BOLD}{C.BCYAN}║{C.RESET}")
        print(f"{C.BOLD}{C.BCYAN}  ╚══════════════════════════════════════════════════════════════╝{C.RESET}")
        ts = self.now.strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"  {C.DIM}Last updated: {ts}{C.RESET}")

    # ─── Portfolio Summary ───────────────────────────────────────
    def _render_portfolio(self):
        self._header("PORTFOLIO")

        from config import BANKROLL, CASH_RESERVE_PCT

        # Get trade stats
        trades = self.conn.execute(
            "SELECT * FROM trades WHERE status='open' AND (is_paper IS NULL OR is_paper=0)"
        ).fetchall()
        closed = self.conn.execute(
            "SELECT * FROM trades WHERE status!='open' AND (is_paper IS NULL OR is_paper=0)"
        ).fetchall()

        deployed = sum(float(t["cost_basis"] or 0) for t in trades)
        realized = sum(float(t["realized_pnl"] or 0) for t in closed)
        unrealized = sum(float(t["unrealized_pnl"] or 0) for t in trades)
        total_pnl = realized + unrealized
        cash = BANKROLL + realized - deployed
        cash_pct = cash / BANKROLL if BANKROLL > 0 else 0

        # Bankroll bar
        bankroll_now = BANKROLL + total_pnl
        print(f"  {C.BOLD}Bankroll{C.RESET}     ${bankroll_now:>8.2f}  {bar(min(bankroll_now / BANKROLL, 1.0))}")
        print(f"  {C.BOLD}Cash{C.RESET}         ${cash:>8.2f}  {C.DIM}({cash_pct:.0%} reserve){C.RESET}"
              f"  {'  ' + C.BGREEN + 'OK' + C.RESET if cash_pct >= CASH_RESERVE_PCT else '  ' + C.BRED + 'LOW' + C.RESET}")
        print(f"  {C.BOLD}Deployed{C.RESET}     ${deployed:>8.2f}  {C.DIM}across {len(trades)} position(s){C.RESET}")
        print()
        print(f"  {C.BOLD}Realized P&L{C.RESET}   {pnl_str(realized)}")
        print(f"  {C.BOLD}Unrealized{C.RESET}     {pnl_str(unrealized)}")
        print(f"  {C.BOLD}Total P&L{C.RESET}      {pnl_str(total_pnl)}")

        # Drawdown
        peak = BANKROLL  # simplified — track real peak over time
        dd = (peak - bankroll_now) / peak * 100 if peak > 0 else 0
        dd_color = C.BGREEN if dd < 5 else (C.BYELLOW if dd < 20 else C.BRED)
        mode = "NORMAL"
        mode_color = C.BGREEN
        if dd >= 35:
            mode = "HALTED"
            mode_color = C.BRED
        elif dd >= 20:
            mode = "DEFENSIVE"
            mode_color = C.BYELLOW

        print(f"\n  {C.BOLD}Drawdown{C.RESET}     {dd_color}{dd:.1f}%{C.RESET}  "
              f"{C.BOLD}Mode:{C.RESET} {mode_color}{C.BOLD}{mode}{C.RESET}")
        self._footer()

    # ─── Open Trades (Live) ──────────────────────────────────────
    def _render_open_trades(self):
        trades = self.conn.execute(
            "SELECT * FROM trades WHERE status='open' AND (is_paper IS NULL OR is_paper=0) ORDER BY opened_at DESC"
        ).fetchall()

        self._header(f"LIVE TRADES ({len(trades)})")

        if not trades:
            print(f"  {C.DIM}No live trades open.{C.RESET}")
            self._footer()
            return

        # Header row
        print(f"  {C.BOLD}{C.CYAN}{'Market':<35} {'Side':<8} {'Entry':>7} {'Now':>7} {'P&L':>10} {'Age'}{C.RESET}")
        print(f"  {C.DIM}{'─'*80}{C.RESET}")

        for t in trades:
            q = truncate(t["question"] or t["condition_id"], 34)
            side = t["side"] or "?"
            entry = float(t["entry_price"] or 0)
            current = float(t["current_price"] or entry)
            pnl = float(t["unrealized_pnl"] or 0)
            opened = t["opened_at"] or ""

            # Calculate age
            try:
                opened_dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                age_hrs = (self.now - opened_dt).total_seconds() / 3600
                age_str = f"{age_hrs:.0f}h" if age_hrs < 48 else f"{age_hrs/24:.0f}d"
            except Exception:
                age_str = "?"

            side_color = C.BGREEN if "YES" in side.upper() else C.BRED
            print(f"  {q:<35} {side_color}{side:<8}{C.RESET} "
                  f"${entry:>5.3f} ${current:>5.3f} {pnl_str(pnl):>18} {C.DIM}{age_str}{C.RESET}")

        self._footer()

    # ─── Paper Trades ────────────────────────────────────────────
    def _render_paper_trades(self):
        trades = self.conn.execute(
            "SELECT * FROM trades WHERE is_paper=1 AND status='open' ORDER BY opened_at DESC"
        ).fetchall()
        closed_paper = self.conn.execute(
            "SELECT * FROM trades WHERE is_paper=1 AND status!='open'"
        ).fetchall()

        total_open = len(trades)
        total_closed = len(closed_paper)
        paper_deployed = sum(float(t["cost_basis"] or 0) for t in trades)
        paper_realized = sum(float(t["realized_pnl"] or 0) for t in closed_paper)
        paper_unrealized = sum(float(t["unrealized_pnl"] or 0) for t in trades)

        self._header(f"PAPER TRADES ({total_open} open, {total_closed} closed)")

        if not trades and not closed_paper:
            print(f"  {C.DIM}No paper trades yet. Run: python run.py paper-trade --top 20{C.RESET}")
            self._footer()
            return

        print(f"  {C.BOLD}Paper Deployed{C.RESET}  ${paper_deployed:>8.2f}")
        print(f"  {C.BOLD}Paper P&L{C.RESET}       {pnl_str(paper_realized + paper_unrealized)}")
        print()

        # Header row
        print(f"  {C.BOLD}{C.MAGENTA}{'Market':<35} {'Side':<8} {'Entry':>7} {'Now':>7} {'P&L':>10}{C.RESET}")
        print(f"  {C.DIM}{'─'*72}{C.RESET}")

        for t in trades[:10]:  # Show top 10
            q = truncate(t["question"] or t["condition_id"], 34)
            side = t["side"] or "?"
            entry = float(t["entry_price"] or 0)
            current = float(t["current_price"] or entry)
            pnl = float(t["unrealized_pnl"] or 0)

            side_color = C.BGREEN if "YES" in side.upper() else C.BRED
            print(f"  {q:<35} {side_color}{side:<8}{C.RESET} "
                  f"${entry:>5.3f} ${current:>5.3f} {pnl_str(pnl):>18}")

        if total_open > 10:
            print(f"  {C.DIM}... and {total_open - 10} more{C.RESET}")

        self._footer()

    # ─── Market Overview ─────────────────────────────────────────
    def _render_market_overview(self):
        stats = db_stats(self.conn)

        self._header("MARKET OVERVIEW")

        active = stats.get("active_markets", 0)
        resolved = stats.get("resolved_markets", 0)
        total = stats.get("markets", 0)

        print(f"  {C.BOLD}Active Markets{C.RESET}   {C.BWHITE}{active:>5}{C.RESET}")
        print(f"  {C.BOLD}Resolved{C.RESET}         {C.DIM}{resolved:>5}{C.RESET}")
        print(f"  {C.BOLD}Total Tracked{C.RESET}    {C.DIM}{total:>5}{C.RESET}")
        print()

        # Category breakdown
        by_type = stats.get("by_type", {})
        if by_type:
            print(f"  {C.BOLD}By Category:{C.RESET}")
            icons = {
                "political": "🗳 ", "crypto": "₿ ", "sports": "🏆",
                "geopolitical": "🌍", "regulatory": "📋", "other": "📊",
                None: "  ",
            }
            max_count = max(by_type.values()) if by_type else 1
            for cat, count in sorted(by_type.items(), key=lambda x: -x[1]):
                icon = icons.get(cat, "  ")
                cat_name = (cat or "uncategorized").capitalize()
                bar_w = int(count / max_count * 25) if max_count > 0 else 0
                print(f"    {icon} {cat_name:<15} {C.BCYAN}{'█' * bar_w}{C.RESET} {count}")

        self._footer()

    # ─── Signal Health ───────────────────────────────────────────
    def _render_signal_health(self):
        self._header("SIGNAL STATUS")

        from config import NEWSAPI_KEY, MIROFISH_API_URL

        signals = [
            ("Sharp Traders", "7 whale wallets", True, C.BGREEN),
            ("Base Rate", "Historical calibration", True, C.BGREEN),
            ("Cross-Platform", "Metaculus/Manifold", True, C.BGREEN),
            ("News Sentiment", "NewsAPI", bool(NEWSAPI_KEY), C.BGREEN if NEWSAPI_KEY else C.BYELLOW),
            ("MiroFish Swarm", f"Docker @ {MIROFISH_API_URL}", False, C.BRED),
        ]

        # Check MiroFish
        try:
            import urllib.request
            req = urllib.request.urlopen(f"{MIROFISH_API_URL}/health", timeout=2)
            if req.status == 200:
                signals[4] = ("MiroFish Swarm", f"Docker @ {MIROFISH_API_URL}", True, C.BGREEN)
        except Exception:
            pass

        for name, desc, active, color in signals:
            status = f"{C.BGREEN}● ONLINE{C.RESET}" if active else f"{C.BRED}○ OFFLINE{C.RESET}"
            print(f"  {status}  {C.BOLD}{name:<20}{C.RESET} {C.DIM}{desc}{C.RESET}")

        # Calibration stats
        row = self.conn.execute(
            "SELECT COUNT(*) as n FROM resolutions WHERE brier_score_ours IS NOT NULL"
        ).fetchone()
        cal_n = row["n"] if row else 0

        brier_row = self.conn.execute("""
            SELECT AVG(brier_score_ours) as ours, AVG(brier_score_market) as mkt
            FROM resolutions WHERE brier_score_ours IS NOT NULL
        """).fetchone()

        print()
        print(f"  {C.BOLD}Calibration:{C.RESET}  {cal_n} resolved predictions")
        if brier_row and brier_row["ours"]:
            ours = brier_row["ours"]
            mkt = brier_row["mkt"]
            improvement = ((mkt - ours) / mkt * 100) if mkt and mkt > 0 else 0
            imp_color = C.BGREEN if improvement > 0 else C.BRED
            print(f"  {C.BOLD}Our Brier{C.RESET}     {C.BCYAN}{ours:.4f}{C.RESET}  "
                  f"{C.DIM}vs market {mkt:.4f}{C.RESET}  "
                  f"{imp_color}{improvement:+.1f}%{C.RESET}")

        self._footer()

    # ─── Recent Activity ─────────────────────────────────────────
    def _render_recent_activity(self):
        self._header("RECENT ACTIVITY")

        # Recent predictions
        preds = self.conn.execute("""
            SELECT question, our_estimate, market_price_at, delta, confidence, predicted_at
            FROM predictions ORDER BY predicted_at DESC LIMIT 5
        """).fetchall()

        if preds:
            for p in preds:
                q = truncate(p["question"] or "?", 35)
                est = float(p["our_estimate"] or 0)
                mkt = float(p["market_price_at"] or 0)
                delta = float(p["delta"] or 0)
                conf = (p["confidence"] or "?").upper()
                delta_color = C.BGREEN if abs(delta) >= 0.05 else C.DIM

                conf_color = {
                    "HIGH": C.BGREEN, "MEDIUM": C.BYELLOW, "LOW": C.BRED
                }.get(conf, C.DIM)

                ts = ""
                try:
                    dt = datetime.fromisoformat((p["predicted_at"] or "").replace("Z", "+00:00"))
                    age = (self.now - dt).total_seconds() / 3600
                    ts = f"{age:.0f}h ago" if age < 48 else f"{age/24:.0f}d ago"
                except Exception:
                    pass

                print(f"  {delta_color}{'▲' if delta > 0 else '▼' if delta < 0 else '─'}{C.RESET} "
                      f"{q} "
                      f"{C.DIM}est={est:.2f} mkt={mkt:.2f}{C.RESET} "
                      f"{delta_color}{delta:+.2f}{C.RESET} "
                      f"{conf_color}[{conf}]{C.RESET} "
                      f"{C.DIM}{ts}{C.RESET}")
        else:
            print(f"  {C.DIM}No predictions yet. Run: python run.py scan --top 20{C.RESET}")

        self._footer()

    # ─── Footer ──────────────────────────────────────────────────
    def _render_footer(self):
        print()
        print(f"  {C.DIM}Commands:{C.RESET}")
        print(f"    {C.CYAN}python run.py scan --top 20{C.RESET}    {C.DIM}Scan for opportunities{C.RESET}")
        print(f"    {C.CYAN}python run.py paper-trade{C.RESET}      {C.DIM}Run paper trading session{C.RESET}")
        print(f"    {C.CYAN}python run.py paper-report{C.RESET}     {C.DIM}Check paper trade results{C.RESET}")
        print(f"    {C.CYAN}python run.py fetch{C.RESET}            {C.DIM}Fetch fresh market data{C.RESET}")
        print(f"    {C.CYAN}python run.py dashboard{C.RESET}        {C.DIM}Refresh this view{C.RESET}")
        print()
        print(f"  {C.BOLD}{C.BCYAN}  ─── POLYMARKET AGENT v1.0 ── Phase 4 Complete ───{C.RESET}")
        print()


def main():
    dash = Dashboard()
    try:
        dash.render()
    finally:
        dash.close()


if __name__ == "__main__":
    main()

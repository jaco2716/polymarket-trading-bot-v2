#!/usr/bin/env python3
"""
Polymarket Trading Bot v2 -- Entry Point

Modes:
  shadow   -- hypothetical trades only, lower thresholds
  compete  -- paper trades, strategies share slots
  parallel -- paper trades, independent slots
  live     -- real CLOB orders
"""
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone

from config import CFG, validate_config
from utils.logging import setup_logging
from utils.db import init_db
import utils.discarded as discarded
from clients.clob_client import PolymarketClient
from clients.anthropic_client import HaikuClient
from services.budget_service import BudgetService
from services.market_service import MarketService
from services.execution_service import ExecutionService
from services.resolution_service import ResolutionService
from services.copy_trading_service import CopyTradingService
from strategies.haiku_strategy import HaikuStrategy
from strategies.whale_strategy import WhaleStrategy
from utils.filters import extract_event_key

log = logging.getLogger(__name__)


class ScanOrchestrator:
    """Orchestrates a single scan cycle."""

    def __init__(
        self,
        market_service: MarketService,
        execution_service: ExecutionService,
        resolution_service: ResolutionService,
        copy_trading_service: CopyTradingService,
        budget_service: BudgetService,
        haiku_strategy: HaikuStrategy,
        whale_strategy: WhaleStrategy,
    ):
        self._markets = market_service
        self._execution = execution_service
        self._resolution = resolution_service
        self._copy_trading = copy_trading_service
        self._budget = budget_service
        self._haiku = haiku_strategy
        self._whale = whale_strategy

    def run_scan(self, con) -> None:
        """
        One complete scan cycle:
          1. Resolve settled trades (both real and shadow)
          2. Check open trade limits
          3. Fetch markets
          4. Run haiku strategy (priority, first)
          5. Run whale strategy (fetched only after haiku completes)
          6. Log scan results

        ATOMIC ACTION PATTERN: When haiku returns a signal above threshold,
        execute() is called IMMEDIATELY before evaluating the next market.
        """
        mode = CFG["STRATEGY_MODE"]
        budget_mode = self._budget.get_budget_mode()
        budget = self._budget.load(budget_mode)
        log.info(f"\n{'═'*55}\n  🔍 Starting scan  |  Budget: ${budget:.2f}  |  mode={mode}\n{'═'*55}")

        # Step 1: Resolve
        self._resolution.resolve_settled_trades(con)
        self._resolution.resolve_shadow_trades(con)

        # Step 2: Check limits
        if mode == "live":
            if self._budget.open_trade_count(con, mode_filter="live") >= CFG["LIVE_MAX_OPEN_TRADES"]:
                log.info(f"At max live open trades ({CFG['LIVE_MAX_OPEN_TRADES']}) — skipping new entries this scan")
                return
        elif mode != "shadow" and self._budget.open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
            log.info("At max open trades — skipping new entries this scan")
            return

        # Step 3: Fetch markets
        markets = self._markets.fetch_markets(randomize=(mode == "shadow"))
        if not markets:
            log.warning("No markets returned — API may be down")
            return

        shadow = (mode == "shadow")
        traded: set = (
            self._budget.open_shadow_market_ids(con) if shadow
            else self._budget.open_market_ids(con)
        )
        signals_found = 0
        trades_placed = 0

        # Step 4: Haiku strategy (priority, runs first)
        if CFG["ENABLE_HAIKU"]:
            h_signals, h_trades = self._run_haiku_scan(con, markets, traded, shadow=shadow)
            signals_found += h_signals
            trades_placed += h_trades

        # Step 5: Whale strategy (fetched only after haiku completes)
        if CFG["ENABLE_WHALE_COPY"]:
            log.info("Checking whale wallets...")
            whale_markets = self._markets.fetch_markets(randomize=False)
            whale_signals = self._copy_trading.get_whale_signals(
                whale_markets if whale_markets else markets
            )

            whale_cap = CFG["MAX_WHALE_TRADES_PER_SCAN"]
            if shadow:
                fresh = [s for s in whale_signals if s["market"]["id"] not in traded and not discarded.is_discarded(s["market"]["id"])]
                for sig in fresh[:whale_cap]:
                    trade_req = self._whale.from_signal(sig)
                    self._execution.execute(con, trade_req)
                    signals_found += 1
                    trades_placed += 1
                    traded.add(sig["market"]["id"])
            else:
                fresh = [
                    s for s in whale_signals
                    if s["market"]["id"] not in traded
                    and not discarded.is_discarded(s["market"]["id"])
                    and not self._execution._poly.is_neg_risk(s["market"], s["direction"])
                ]
                for sig in fresh[:whale_cap]:
                    trade_req = self._whale.from_signal(sig)
                    result = self._execution.execute(con, trade_req)
                    signals_found += 1
                    if result is not None:
                        trades_placed += 1
                        traded.add(sig["market"]["id"])

        # Step 6: Log scan
        con.execute(
            "INSERT INTO scan_log (ts, markets_checked, signals_found, trades_placed) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), len(markets), signals_found, trades_placed),
        )
        con.commit()
        log.info(f"Scan complete: {len(markets)} markets checked, {signals_found} signals, {trades_placed} trades placed")

    def _run_haiku_scan(self, con, markets: list[dict], traded: set, shadow: bool) -> tuple[int, int]:
        """Run haiku strategy over markets. Returns (signals, trades)."""
        signals = trades = 0
        scan_limit = CFG["MARKETS_PER_SCAN"]

        # Load event keys of all open positions to block correlated siblings
        traded_event_keys: set[str] = self._budget.open_event_keys(con) if not shadow else set()

        log.info(f"[haiku-analyse] Scanning top {min(scan_limit, len(markets))} markets...")
        for m in markets[:scan_limit]:
            if m["id"] in traded or discarded.is_discarded(m["id"]):
                continue
            if not shadow and not self._execution._can_open_trade(con):
                break

            # Skip markets that are correlated with an already-open position
            event_key = extract_event_key(m["name"])
            if event_key in traded_event_keys:
                log.debug(f"[haiku] Skipping same-event market: {m['name'][:70]}")
                continue

            trade_req = self._haiku.evaluate(con, m, shadow=shadow)
            if trade_req is None:
                continue

            signals += 1
            # ATOMIC: execute IMMEDIATELY, before next market
            result = self._execution.execute(con, trade_req)
            if result is not None or shadow:
                trades += 1
                traded.add(m["id"])
                traded_event_keys.add(event_key)  # block siblings in same scan
                break  # one haiku trade per scan
        return signals, trades


def print_stats(con):
    """Print performance statistics."""
    from services.budget_service import BudgetService
    budget_svc = BudgetService()

    rows = con.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) as resolved,
            SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) as open,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(COALESCE(pnl, 0)), 2) as total_pnl,
            ROUND(SUM(fee), 4) as total_fees
        FROM trades
    """).fetchone()

    total, resolved, open_c, wins, pnl, fees = rows
    pnl = pnl or 0.0
    fees = fees or 0.0
    win_rate = f"{wins/resolved*100:.1f}%" if resolved else "n/a"
    budget = budget_svc.load("paper")

    log.info(
        f"\n{'━'*55}\n"
        f"  📊 Stats  |  Budget: ${budget:.2f}  |  P&L: {pnl:+.2f} USDC\n"
        f"  Trades: {total} total  |  {open_c} open  |  {resolved} resolved\n"
        f"  Win rate: {win_rate}  |  Fees paid: {fees:.4f} USDC\n"
        f"{'━'*55}"
    )

    # Per-tag breakdown
    tag_rows = con.execute(
        "SELECT tags, pnl FROM trades WHERE resolved=1 AND pnl IS NOT NULL"
    ).fetchall()

    tag_stats: dict = {}
    for tags_json, t_pnl in tag_rows:
        for tag in json.loads(tags_json):
            s = tag_stats.setdefault(tag, {"count": 0, "pnl": 0.0, "wins": 0})
            s["count"] += 1
            s["pnl"] += t_pnl
            if t_pnl > 0:
                s["wins"] += 1

    if tag_stats:
        log.info("  Strategy breakdown:")
        for tag, s in sorted(tag_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr = f"{s['wins']/s['count']*100:.0f}%" if s["count"] else "—"
            log.info(f"    [{tag}]  {s['count']} trades  wr={wr}  P&L={s['pnl']:+.2f}")

    # Shadow trade comparison
    shadow_rows = con.execute("""
        SELECT strategy,
               COUNT(*) as total,
               SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) as resolved,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(COALESCE(pnl, 0)), 2) as total_pnl
        FROM shadow_trades
        GROUP BY strategy
    """).fetchall()
    if shadow_rows:
        log.info("  Shadow strategy comparison (hypothetical, no real budget):")
        for strategy, total, resolved, wins, spnl in sorted(shadow_rows, key=lambda x: -(x[4] or 0)):
            wr = f"{wins/resolved*100:.0f}%" if resolved else "n/a"
            log.info(f"    [{strategy}]  {total} evaluated  wr={wr}  hypothetical P&L={spnl:+.2f}")


def main():
    """Entry point."""
    setup_logging()
    mode = CFG["STRATEGY_MODE"]

    if mode == "live":
        log.info("━" * 55)
        log.info("  Polymarket Trading Bot v2  — LIVE MODE (REAL MONEY)")
        log.info("━" * 55)
        if CFG["LIVE_DRY_RUN"]:
            log.info("  Dry-run enabled — orders logged but NOT placed")
    else:
        log.info("━" * 55)
        log.info("  Polymarket Paper Trading Bot v2  (paper money only)")
        log.info("━" * 55)

    if not CFG["ANTHROPIC_API_KEY"] and CFG["ENABLE_HAIKU"]:
        log.error("ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    # Validate config
    errors = validate_config()
    if errors:
        for e in errors:
            log.error(f"Config error: {e}")
        sys.exit(1)

    # Construct service graph (dependency injection)
    polymarket_client = PolymarketClient()
    haiku_client = HaikuClient()
    budget_service = BudgetService()
    market_service = MarketService()
    execution_service = ExecutionService(polymarket_client, budget_service)
    resolution_service = ResolutionService(budget_service)
    copy_trading_service = CopyTradingService(polymarket_client)
    haiku_strategy = HaikuStrategy(haiku_client)
    whale_strategy = WhaleStrategy()

    orchestrator = ScanOrchestrator(
        market_service=market_service,
        execution_service=execution_service,
        resolution_service=resolution_service,
        copy_trading_service=copy_trading_service,
        budget_service=budget_service,
        haiku_strategy=haiku_strategy,
        whale_strategy=whale_strategy,
    )

    # Init DB
    con = init_db()

    # Live pre-flight
    if mode == "live":
        try:
            polymarket_client.validate_setup()
        except RuntimeError as e:
            log.error(f"Live trading setup failed: {e}")
            sys.exit(1)
        polymarket_client.repair_open_trade_prices(con)

    try:
        budget = budget_service.load(budget_service.get_budget_mode())
        log.info(f"Starting budget: ${budget:.2f} USDC")
        log.info(f"Strategies: haiku={CFG['ENABLE_HAIKU']}, whale_copy={CFG['ENABLE_WHALE_COPY']}, mode={mode}")
        max_open = CFG["LIVE_MAX_OPEN_TRADES"] if mode == "live" else CFG["MAX_OPEN_TRADES"]
        interval_display = CFG["SHADOW_SCAN_INTERVAL"] if mode == "shadow" else CFG["SCAN_INTERVAL"]
        log.info(f"Scan interval: {interval_display}s  |  Max open trades: {max_open}")
        log.info(f"Trade size: {CFG['TRADE_SIZE_PCT']*100:.0f}% of budget per trade (~${budget*CFG['TRADE_SIZE_PCT']:.2f})")
        if mode == "live":
            log.info(f"Live hard cap: ${CFG['MAX_LIVE_TRADE_USDC']:.2f} per trade  |  Dry run: {CFG['LIVE_DRY_RUN']}")

        scan_count = 0
        while True:
            try:
                current_budget = budget_service.load(budget_service.get_budget_mode())
                trade_amount = current_budget * CFG["TRADE_SIZE_PCT"]

                if trade_amount < CFG["MIN_TRADE_USDC"]:
                    cooldown = CFG["LOW_BUDGET_COOLDOWN"]
                    interval = CFG["SHADOW_SCAN_INTERVAL"] if mode == "shadow" else CFG["SCAN_INTERVAL"]
                    log.info(
                        f"Budget too low for new trades (${current_budget:.2f}, "
                        f"trade size ${trade_amount:.2f} < ${CFG['MIN_TRADE_USDC']:.2f}) "
                        f"— cooldown for {cooldown}s, resolving every {interval}s"
                    )
                    elapsed = 0
                    while elapsed < cooldown:
                        resolution_service.resolve_settled_trades(con)
                        resolution_service.resolve_shadow_trades(con)
                        current_budget = budget_service.load(budget_service.get_budget_mode())
                        if current_budget * CFG["TRADE_SIZE_PCT"] >= CFG["MIN_TRADE_USDC"]:
                            log.info(f"Budget recovered to ${current_budget:.2f} — resuming scans")
                            break
                        time.sleep(interval)
                        elapsed += interval
                    continue

                orchestrator.run_scan(con)
                scan_count += 1
                if scan_count % 12 == 0:
                    print_stats(con)
            except KeyboardInterrupt:
                log.info("Stopped by user.")
                print_stats(con)
                break
            except Exception:
                log.error(f"Scan error:\n{traceback.format_exc()}")

            interval = CFG["SHADOW_SCAN_INTERVAL"] if mode == "shadow" else CFG["SCAN_INTERVAL"]
            log.info(f"Sleeping {interval}s until next scan...")
            time.sleep(interval)
    finally:
        con.close()


if __name__ == "__main__":
    main()

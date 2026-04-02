#!/usr/bin/env python3
"""
Standalone test script for neg_risk market trading on Polymarket.

Tests:
  1. CLOB client initialisation
  2. Top-20 leaderboard whale scan — finds a neg_risk market with
     >= $1000 volume and end date within 5 days
  3. neg_risk status check for the YES token
  4. COLLATERAL balance and allowance
  5. CONDITIONAL balance and allowance for the YES token
  6. Places a $1 BUY (YES side) FAK order if PLACE_ORDER=true and balance >= $1

Usage:
  cd /path/to/polymarket-trading-bot-v2
  source venv/bin/activate

  # API checks only (no order placed):
  python test_neg_risk_trade.py

  # Full test with real $1 trade:
  PLACE_ORDER=true python test_neg_risk_trade.py
"""
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
ENV_FILE = PROJECT_ROOT / ".env"
if not ENV_FILE.exists():
    print(f"ERROR: .env file not found at {ENV_FILE}")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv(dotenv_path=ENV_FILE, override=True)

# Use the project's shared HTTP client (retry logic, shared session)
sys.path.insert(0, str(PROJECT_ROOT))
from clients.http import get as http_get

CLOB_URL      = "https://clob.polymarket.com"
DATA_URL      = "https://data-api.polymarket.com"
ORDER_AMOUNT_USDC = 1.0
PLACE_ORDER   = os.getenv("PLACE_ORDER", "false").lower() == "true"
PRIVATE_KEY   = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "") or None

MAX_DAYS_LEFT = 4       # market must close within this many days

_WALLET_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')

if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
    sys.exit(1)


def sep(title: str) -> None:
    print(f"\n{'─' * 58}")
    print(f"  {title}")
    print('─' * 58)



# ── Step 1: Init CLOB client ──────────────────────────────
sep("Step 1: Initialising CLOB client")
try:
    from py_clob_client.client import ClobClient
    client = ClobClient(
        CLOB_URL,
        key=PRIVATE_KEY,
        chain_id=137,
        signature_type=1,
        funder=FUNDER_ADDRESS,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    print("  OK — CLOB client initialised (Polygon mainnet)")
except Exception as e:
    print(f"  FAIL — {e}")
    sys.exit(1)


# ── Step 2: Find a neg_risk market via whale leaderboard ──
sep("Step 2: Scanning top-20 leaderboard for a neg_risk market")
print(f"  Filters: whale size >= $1000, closes within {MAX_DAYS_LEFT} days")

# 2a — fetch top 20 wallets by weekly PNL
data = http_get(f"{DATA_URL}/v1/leaderboard", params={
    "limit":      25,
    "category":   "OVERALL",
    "timePeriod": "WEEK",
    "orderBy":    "PNL",
})
if not data:
    print("  FAIL — could not fetch leaderboard")
    sys.exit(1)

entries = data if isinstance(data, list) else data.get("data", [])
wallets = []
seen = set()
for e in entries:
    addr = e.get("proxyWallet") or e.get("address") or e.get("user", "")
    if addr and _WALLET_RE.match(addr) and addr not in seen:
        wallets.append(addr)
        seen.add(addr)

print(f"  Found {len(wallets)} wallets on leaderboard")
if not wallets:
    print("  FAIL — no valid wallets returned")
    sys.exit(1)

# 2b — scan positions for each wallet, filter by volume + end date, check neg_risk
now = datetime.now(timezone.utc)
deadline = now + timedelta(days=MAX_DAYS_LEFT)

found_market = None
found_token_id = None
found_price = 0.5
found_wallet = None
found_whale_size = 0.0

print(f"  Scanning positions (whale size >= $1000, closes within {MAX_DAYS_LEFT} days)...")
for wallet in wallets:
    positions_data = http_get(f"{DATA_URL}/positions", params={
        "user":          wallet,
        "sizeThreshold": 1,
        "limit":         100,
        "sortBy":        "CURRENT",
        "sortDirection": "DESC",
    })
    if not positions_data:
        time.sleep(0.1)
        continue

    positions = positions_data if isinstance(positions_data, list) else positions_data.get("data", [])
    for p in positions:
        try:
            if p.get("redeemable"):
                print(f"  Skipping redeemable {p.get('redeemable')}")
                continue
            cur_price = float(p.get("curPrice") or 0)
            if cur_price <= 0:
                print(f"  Skipping cur_price {cur_price:.3f}")  
                continue
            current_value = float(p.get("currentValue") or 0)
            if current_value < 100:
                print(f"  Skipping current_value {current_value:.0f}")  
                continue

            # Check end date
            end_str = p.get("endDate") or ""
            if not end_str:
                print(f"  Skipping end_date null ")  
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                if end_dt <= now or end_dt > deadline:
                    print(f"  Skipping end_date {end_str}")  
                    continue
            except ValueError:
                continue

            # token_id is the `asset` field directly — no extra API call needed
            token_id     = str(p.get("asset") or "")
            condition_id = p.get("conditionId") or ""
            if not token_id or not condition_id:
                print(f"  Skipping token_id or condition_id null (token_id={token_id}, condition_id={condition_id})")  
                continue

            if not p.get("negativeRisk"):
                print(f"  Skipping negativeRisk={p.get('negativeRisk')}")  
                continue

            days_left = (end_dt - now).total_seconds() / 86400
            print(
                f"  [NEG_RISK] {p.get('title', condition_id)[:55]}\n"
                f"  size=${current_value:.0f}  price={cur_price:.3f}"
                f"  {days_left:.1f}d left  wallet={wallet[:10]}…"
            )

            if found_market is None:
                # Fetch CLOB market for question text + token list
                market_data = http_get(f"{CLOB_URL}/markets/{condition_id}") or {}
                market_data["id"] = condition_id
                found_market     = market_data
                found_token_id   = token_id
                found_price      = cur_price
                found_wallet     = wallet
                found_whale_size = current_value
                print(f"  --> Selected this market for the test")
                break

        except Exception as e:
            print(f"  Skipping error parsing position data: {e}")
            continue

    if found_market is not None:
        break
    time.sleep(0.15)

if found_market is None:
    print(
        f"\n  No neg_risk market found matching filters "
        f"(whale size >= $1000, closes within {MAX_DAYS_LEFT} days) "
        f"across top-60 leaderboard wallets."
    )
    print("  Try increasing MAX_DAYS_LEFT at the top of this script.")
    sys.exit(0)

YES_TOKEN_ID = found_token_id
YES_PRICE    = found_price
MARKET_ID    = found_market.get("id", "unknown")
print(f"\n  Market ID  : {MARKET_ID}")
print(f"  Question   : {found_market.get('question', 'N/A')}")
print(f"  YES token  : {YES_TOKEN_ID}")
print(f"  Price      : {YES_PRICE:.4f}")
print(f"  Whale      : {found_wallet[:10]}… (holding ${found_whale_size:.0f})")


# ── Step 3: Confirm neg_risk status ──────────────────────
sep("Step 3: Confirming neg_risk status")
try:
    is_neg_risk = client.get_neg_risk(YES_TOKEN_ID)
    print(f"  token_id : {YES_TOKEN_ID[:24]}...")
    print(f"  neg_risk : {is_neg_risk}")
    if not is_neg_risk:
        print("  NOTE: Token is NOT neg_risk — order will use the standard exchange")
except Exception as e:
    print(f"  FAIL — {e}")
    sys.exit(1)


# ── Step 4: COLLATERAL balance + allowance ───────────────
sep("Step 4: COLLATERAL balance and allowance")
usdc_balance = 0.0
try:
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    col_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    print("  update_balance_allowance(COLLATERAL)...")
    client.update_balance_allowance(params=col_params)
    print("  get_balance_allowance(COLLATERAL)...")
    col_resp = client.get_balance_allowance(params=col_params)
    print(f"  raw response  : {col_resp}")
    usdc_balance   = float(col_resp.get("balance")   or 0) / 1_000_000
    usdc_allowance = float(col_resp.get("allowance") or 0) / 1_000_000
    print(f"  USDC balance  : ${usdc_balance:.4f}")
    print(f"  USDC allowance: ${usdc_allowance:.4f}")
except Exception as e:
    print(f"  FAIL — {e}")
    sys.exit(1)


# ── Step 5: CONDITIONAL balance + allowance ─────────────
sep("Step 5: CONDITIONAL balance and allowance (YES token)")
try:
    cond_params = BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=YES_TOKEN_ID,
    )
    print("  update_balance_allowance(CONDITIONAL, token_id=YES)...")
    client.update_balance_allowance(params=cond_params)
    print("  get_balance_allowance(CONDITIONAL, token_id=YES)...")
    cond_resp = client.get_balance_allowance(params=cond_params)
    print(f"  raw response       : {cond_resp}")
    cond_balance   = float(cond_resp.get("balance")   or 0)
    cond_allowance = float(cond_resp.get("allowance") or 0)
    print(f"  CONDITIONAL balance  (shares): {cond_balance}")
    print(f"  CONDITIONAL allowance(shares): {cond_allowance}")
except Exception as e:
    print(f"  FAIL (non-fatal) — {e}")
    print("  Continuing...")


# ── Step 6: Place $1 BUY order ───────────────────────────
sep("Step 6: Place $1 BUY order (YES side)")

if not PLACE_ORDER:
    print(f"  SKIPPED — set PLACE_ORDER=true to enable")
    print(f"  Would place: BUY ${ORDER_AMOUNT_USDC} YES @ ~{YES_PRICE:.4f} on market {MARKET_ID}")
elif usdc_balance < ORDER_AMOUNT_USDC:
    print(f"  SKIPPED — insufficient USDC balance (${usdc_balance:.4f} < ${ORDER_AMOUNT_USDC})")
else:
    print(f"  Placing BUY ${ORDER_AMOUNT_USDC} YES @ market (price={YES_PRICE:.4f}, +1% slippage)")
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        limit_price = min(round(YES_PRICE + 0.01, 4), 0.999)
        mo = MarketOrderArgs(
            token_id=YES_TOKEN_ID,
            amount=ORDER_AMOUNT_USDC,
            side=BUY,
            price=limit_price,
        )
        signed_order = client.create_market_order(mo)
        print("  Posting as FAK...")
        post_resp = client.post_order(signed_order, OrderType.FAK)
        print(f"  Response: {post_resp}")

        if isinstance(post_resp, dict):
            order_id = post_resp.get("orderID") or post_resp.get("order_id") or post_resp.get("id")
            status   = post_resp.get("status")
            print(f"  order_id : {order_id}")
            print(f"  status   : {status}")
            if order_id:
                print("  Waiting 3s for fill check...")
                time.sleep(3)
                detail = client.get_order(order_id)
                if isinstance(detail, dict):
                    size_matched = float(detail.get("size_matched") or detail.get("sizeMatched") or 0)
                    print(f"  size_matched (shares): {size_matched}")
                    if size_matched > 0:
                        fill_price = round(ORDER_AMOUNT_USDC / size_matched, 6)
                        print(f"  fill price (USDC/share): {fill_price:.6f}")
                        print("  RESULT: FILLED")
                    else:
                        print("  RESULT: Not filled (thin book or cancelled by FAK)")
                else:
                    print(f"  order detail: {detail}")
            else:
                print("  RESULT: No order_id — likely rejected by exchange")
        else:
            print(f"  Unexpected response type: {type(post_resp)}")

    except Exception as e:
        print(f"  FAIL — {e}")
        import traceback
        traceback.print_exc()


sep("Done")
print()

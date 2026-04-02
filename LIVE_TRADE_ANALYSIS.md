# Live Trade Analysis — Polymarket Trading Bot v2

**Analysis Date:** 2026-04-02  
**Data Range:** 2026-03-31 → 2026-04-02  
**Total Live Trades:** 48 (40 resolved, 8 open)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| Total Trades | 48 |
| Resolved | 40 |
| Open | 8 |
| Wins | 18 |
| Losses | 22 |
| Win Rate | **45.0%** |
| Total Staked | $205.00 |
| Total PnL | **-$21.59** |
| ROI | **-11.58%** |
| Avg PnL / Trade | -$0.55 |
| Avg Entry Price | 0.5678 |
| Strategy | haiku-analyse (100%), whale-copy (2 open, unresolved) |
| All trades direction | **YES only** |

---

## What Went Wrong

### 1. Correlated Trades on the Same Match (Biggest Single Disaster)

The bot placed **4 separate trades on the same T1 vs KT Rolster match** on 2026-04-01, despite having correlation-detection code in the codebase:

| Trade | Market | Stake | PnL |
|-------|--------|-------|-----|
| #28 | Game Handicap: T1 (-1.5) vs KT Rolster (+1.5) | $8.52 | **-$8.52** |
| #29 | LoL: T1 vs KT Rolster - Game 1 Winner | $4.63 | **-$4.62** |
| #30 | LoL: T1 vs KT Rolster (BO3) - LCK Rounds 1-2 | $4.11 | **-$4.11** |
| #31 | LoL: T1 vs KT Rolster - Game 2 Winner | $4.42 | **-$4.41** |
| | **Totals** | **$21.68** | **-$21.66** |

KT Rolster won the match and the bot lost every single bet. The correlation guard (`HAIKU_SKIP_SPORTS`, event key extraction) failed to prevent this. These 4 trades alone account for almost the **entire net loss** of -$21.59. Without this cluster, the account would be near breakeven.

**Root cause:** The event key extraction (`utils/discarded.py`) may not be normalising team names consistently, or the market slugs across game-level and series-level markets use different keys.

---

### 2. Oversized Trade on Trade #26

Trade #26 (CS: Heroic vs BetBoom) was staked at **$18.00** — roughly **3-4x** the typical stake of $3–5 at that point in the session. This lost the full amount (-$17.995) and is the single largest loss by trade.

**Root cause:** The `TRADE_SIZE_PCT` (5% of budget) would have been calculated against a higher live budget early in the session. As the budget was larger on April 1 morning, the absolute stake was much larger. No max cap per individual trade was enforced at that moment.

---

### 3. Systematic Losses in the 0.50–0.70 Price Bucket

The mid-confidence range (most common entry zone) is heavily loss-making:

| Price Bucket | Trades | W | L | PnL |
|-------------|--------|---|---|-----|
| < 0.10 | 1 | 0 | 1 | -$3.54 |
| 0.10–0.30 | 1 | 1 | 0 | +$18.23 |
| 0.30–0.50 | 7 | 2 | 5 | -$11.44 |
| **0.50–0.70** | **26** | **11** | **15** | **-$28.30** |
| 0.70–0.90 | 4 | 3 | 1 | +$3.46 |
| ≥ 0.90 | 1 | 1 | 0 | $0 (no PnL recorded) |

The bot's sweet spot for entries is 0.50–0.70, but at these prices, the YES side needs to win > ~55% of the time just to break even on fees. The actual win rate in this bucket is 42% (11/26). The Haiku model's confidence signals are **not reliable enough** in the mid-confidence zone.

---

### 4. Short-Duration Crypto Price Bets ("Bitcoin Up or Down")

The bot repeatedly traded on short-window Bitcoin directional markets (e.g., 15-minute or 30-minute windows). These are essentially noise with no signal value from news headlines:

| Market | PnL |
|--------|-----|
| Bitcoin Up or Down — March 31, 1PM ET | -$3.99 |
| Bitcoin Up or Down — March 31, 3PM ET | -$3.84 |
| Bitcoin Up or Down — April 1, 9:30AM–9:45AM ET | -$3.54 |
| Bitcoin Up or Down — April 1, 4:10PM–4:15PM ET | -$2.86 |
| Bitcoin Up or Down — April 1, 4AM–8AM ET | +$4.17 ✓ |

**Net from short-duration crypto bets: -$10.06.** These markets resolve before any news analysis could possibly be meaningful (15 minutes). The single win was likely luck.

---

### 5. Esports Negative PnL (Haiku Has No Edge Here)

| Category | Trades | W | L | PnL |
|----------|--------|---|---|-----|
| Esports | 25 | 11 | 14 | **-$15.14** |
| Crypto/Finance | 11 | 6 | 5 | **+$3.02** |
| Other (politics, football) | 4 | 1 | 3 | **-$9.47** |

Esports markets are particularly hard for news-driven analysis. Match outcomes depend on player form, live drafts, and in-game performance — none of which is captured in news headlines. The Haiku model has no structural edge in esports.

---

### 6. Ghost Trades with Zero Fill

Two trades recorded **$0.00 amount and $0.00 PnL** despite being marked resolved with outcome "no":

- Trade #45: CS: Nemiga vs Team Nemesis — outcome `no`, pnl `0.0`
- Trade #48: CS: Keyd vs ODDIK — outcome `no`, pnl `0.0`

These appear to be orders that were placed but never filled (the pending-fill-check logic in `execution_service.py`). They pollute the trade log and misrepresent the actual exposure.

---

### 7. Budget Degradation Forcing Tiny Stakes

The live budget started the session at ~$84 and has been depleted significantly:

| Date | Trades | Staked | Day PnL |
|------|--------|--------|---------|
| 2026-03-31 | 16 | $75.85 | **+$21.31** |
| 2026-04-01 | 28 | $122.68 | **-$42.90** |
| 2026-04-02 | 4 (open) | $6.47 | — |

By April 2, trade sizes have dropped to $1.50–$1.74, offering almost no upside even on winning trades. The bot is still trading when the budget is severely depleted rather than pausing.

---

## What Went Right

### 1. Profitable First Day (March 31)

The first day of live trading was profitable: **+$21.31** on $75.85 staked (**+28% ROI**). The Galions vs Solary pair was the standout:

- Trade #1: LoL Galions vs Solary BO5 → **+$10.15** (0.528 entry, YES won)
- Trade #14: Galions vs Solary Game 3 at **0.18** → **+$18.23** (longshot YES paid out)

The low-price Game 3 trade ($0.18 entry) delivered the highest single-trade return in the dataset.

---

### 2. Crypto/Finance Markets Are Profitable

The Haiku model does have genuine edge in straightforward macro/crypto markets:

- BTC above $66k on Mar 31: +$3.17
- BTC above $68k on Apr 1: +$3.70
- ETH above $2k on Mar 31: +$1.32
- S&P Opens Up on Apr 1: +$6.73
- S&P Up on Apr 1: +$2.15

Total from crypto/finance: **+$3.02** on 11 resolved trades (54% win rate). These are interpretable from public news/sentiment — a natural fit for Haiku's capabilities.

---

### 3. Maker Orders Save on Fees

100% of haiku-analyse trades use `maker` orders, earning a **rebate** (~-$0.003 to -$0.010 per trade) rather than paying a taker fee. Over 48 trades this is meaningful cost savings.

---

### 4. High-Confidence Entries (0.70–0.90) Are Profitable

The 4 trades at 0.70–0.90 price confidence went 3W/1L for **+$3.46**. Restricting to higher-confidence entries is a viable path forward.

---

## Why Are All Positions on 'YES'?

**Every single one of the 48 live trades is direction = `yes`.** This is the most striking anomaly in the dataset.

**Structural Reason — Token ID Architecture:**  
In Polymarket's CLOB, each binary market has two tokens: a YES token and a NO token. The haiku strategy fetches the YES token price and passes it to the Haiku model. When the model returns `direction: "no"`, the code would need to trade the NO token at price `(1 - yes_price)`. Looking at [strategies/haiku_strategy.py](strategies/haiku_strategy.py), only the YES `token_id` is stored in the `trades` table, and the execution logic likely only handles the YES direction for live orders.

**Haiku Model Bias:**  
The model prompt asks Haiku to return `direction: "yes"` or `direction: "no"`. With a confidence floor of 0.50, any market the model assigns ≥50% YES probability will generate a YES trade. Since the price filter already excludes markets below 0.30 or above 0.85, the bot is pre-selecting markets that are "plausibly YES" (priced 0.30–0.85), making it statistically more likely the model agrees with the YES framing.

**Practical Impact:**  
The bot cannot take the contrarian view. When news is **bearish** on a YES-dominated market (e.g., Bitcoin failing to hit a price target), it should buy the NO side. Instead it either skips the market or still buys YES. This halves the available edge.

---

## What Should Be Changed

### 1. Fix Correlated Trade Prevention (Critical)
The T1/KT disaster should never have happened. The event-key deduplication needs to be verified to handle:
- Series-level markets (`T1 vs KT Rolster (BO3)`)
- Game-level markets (`T1 vs KT Rolster - Game 1 Winner`, `Game 2 Winner`)
- Handicap markets (`Game Handicap: T1 (-1.5) vs KT Rolster`)

All four should share the same event key. Add an integration test for this exact case.

### 2. Ban Short-Duration Price Bets (< 1 hour to resolve)
Any market with `end_date - now < 1 hour` should be filtered out in `utils/filters.py`. These markets are pure noise for a news-based strategy. This alone would save ~$10.

### 3. Implement NO-Side Trading
The live execution path (`_execute_live`) needs to handle `direction="no"` by looking up and using the NO token ID. The market data already contains both tokens. Without this the strategy is blind to ~50% of potential edges.

### 4. Add Per-Trade Maximum Cap for Live Mode
Currently `MAX_LIVE_TRADE_USDC=50` in config but it needs to be enforced as an **upper bound**, not just a budget fraction. Trade #26 at $18 was too large for the risk tolerance of a ~$84 account. Suggest `MAX_LIVE_TRADE_USDC=10`.

### 5. Add a Minimum Budget Threshold — Stop Trading When Budget Is Too Low
When the live budget drops below e.g. $20, pause live trading entirely. Trades at $1.50–$2 stake are statistically meaningless and just generate API costs.

### 6. Fix Ghost Trade Recording
Trades with `amount=0` (cancelled/unfilled orders) should either be deleted or have a dedicated `status="cancelled"` field. Counting them as `resolved=1, outcome="no", pnl=0` artificially inflates the loss count.

---

## What Could Be Improved

### 1. Raise Confidence Threshold for Esports
The current `HAIKU_MIN_CONF=0.50` is too permissive for esports. Consider `0.65` for esports specifically, or disable esports entirely (using `HAIKU_SKIP_SPORTS=true`) until a sports-specific data source (team stats, recent form) is added. Haiku has no structural advantage in these markets from news alone.

### 2. Score Markets by Haiku Win Rate Per Category
Track historical win rate per market category in the DB and dynamically adjust the confidence floor per category. Crypto/finance deserves a lower floor (strong edge); esports deserves a higher floor or exclusion.

### 3. Position Sizing by Confidence Band
Instead of flat 5% of budget per trade, scale stake by confidence:
- `conf 0.50–0.60`: 0.5x size
- `conf 0.60–0.70`: 0.75x size
- `conf > 0.70`: 1.0x size

This would have reduced exposure on the many marginal-confidence trades that went on to lose.

### 4. Add Kelly Criterion Sizing for High-Confidence Trades
For trades where Haiku returns `conf > 0.70` and price is outside the 0.45–0.55 midrange, apply a partial Kelly fraction to size up appropriately. The Galions/Solary Game 3 at 0.18 returned 18 USDC from a $4 stake. If that was sized at $10, profit would have been $45+.

### 5. Use Market-Level Win Rate from Shadow Data
The shadow trades table (lower confidence threshold) contains 10 resolved trades. Build feedback loops: if a market type that the shadow strategy wins > 60% of the time, lower the live threshold for that category.

### 6. Add Post-Match Logging for Esports
Log the actual match result separately from trade resolution so the model can identify patterns (e.g., favourites winning BO3s vs BO1s, specific tournaments where upsets are common).

### 7. Deduplicate Bitcoin Range/Direction Overlap
On April 2, the bot placed 3 simultaneous BTC trades:
- BTC above $68k on April 2
- BTC above $66k on April 2  
- BTC between $66k and $68k on April 2

These are **highly correlated** — if BTC is below $66k, all three lose. The correlation guard needs to cover crypto price markets by underlying asset, not just esports event keys.

---

## Notable Patterns

### The Haiku "YES Bias" Is Priced In
Polymarket market makers know that retail traders (and AI bots) tend to bet YES. This means YES prices are often **slightly inflated** relative to true probability. A bot that only bets YES is paying a systematic premium. Adding a no-bias check (e.g., only trade YES if Haiku gives >55% confidence, vs. >50% for NO) would partially correct for this.

### Macro Events Completely Disrupt Short-Term Markets
April 1 was a bad day partly because it was immediately before a major macro event (likely tariff announcements). Bitcoin's price was volatile and unpredictable in short windows. The bot had no awareness of macro event risk and continued trading normally.

### Trade Timing Is Concentrated
Most trades happen within a few hours of market open/resolution. Scan interval of 30 minutes means the bot may be missing better-priced opportunities mid-session.

---

## Key Numbers at a Glance

```
Win Rate:          45.0%   (needs > ~54% to break even at avg 0.57 entry price)
ROI:              -11.6%
Best single trade: +$18.23 (Galions/Solary G3 @ 0.18, trade #14)
Worst single trade:-$18.00 (Heroic vs BetBoom @ 0.56, trade #26)
Biggest loss cluster: T1 vs KT Rolster = -$21.66 (4 trades, 1 match)
Esports PnL:      -$15.14  (11/25 win rate = 44%)
Crypto/Finance:   +$3.02   (6/11 win rate = 55%)
Other:            -$9.47   (1/4 win rate = 25%)
Live budget remaining: ~$84 → depleted ~$20
```

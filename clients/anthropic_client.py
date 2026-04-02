"""
Claude Haiku analysis client. Handles prompt construction, API calls, and response parsing.
"""
import json
import logging
import re
from typing import Optional

import anthropic

from config import CFG

log = logging.getLogger(__name__)

_haiku_client: Optional[anthropic.Anthropic] = None


class HaikuClient:
    """Encapsulates all Anthropic/Haiku interaction."""

    def _get_client(self) -> anthropic.Anthropic:
        """Lazy singleton."""
        global _haiku_client
        if _haiku_client is None:
            if not CFG["ANTHROPIC_API_KEY"]:
                raise RuntimeError("ANTHROPIC_API_KEY not set. Export it before running.")
            _haiku_client = anthropic.Anthropic(api_key=CFG["ANTHROPIC_API_KEY"])
        return _haiku_client

    def _build_system_prompt(self, shadow: bool = False) -> str:
        """Build the system prompt for live or shadow mode."""
        if shadow:
            threshold = CFG["SHADOW_HAIKU_MIN_CONF"]
            return f"""You are a prediction market analyst collecting training data. Evaluate every market honestly.

Reply ONLY with valid JSON — no prose, no markdown fences:
{{
  "edge": true | false,
  "direction": "yes" | "no",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence citing the specific fact or signal behind your view",
  "order_type": "maker" | "taker"
}}

Rules:
- Say edge:true whenever confidence >= {threshold} — this is data collection, not live trading.
- Do not have a directional bias — evaluate YES and NO equally based on evidence alone.
- Always give your honest best-guess direction even when uncertain; we want to learn which signals work.
- Prefer "maker" orders (limit orders) to save on fees."""

        threshold = CFG["HAIKU_MIN_CONF"]
        return f"""You are a prediction market analyst. Given a market and recent news, estimate the probability of the YES outcome resolving true.

Reply ONLY with valid JSON — no prose, no markdown fences:
{{
  "edge": true | false,
  "direction": "yes" | "no",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence citing the specific fact or signal behind your estimate",
  "order_type": "maker" | "taker"
}}

Rules:
- Set confidence to your honest probability estimate for the direction you choose.
- Set edge:true if confidence >= {threshold}, edge:false otherwise.
- Do not have a directional bias — evaluate YES and NO equally based on evidence alone.
- Prefer "maker" orders (limit orders) to save on fees."""

    def _build_user_prompt(self, market: dict, headlines: list[str], live_scores: list[str] = None, asset_context: list[str] = None) -> str:
        """Build the user message with market data, live scores, asset prices, and news."""
        scores_section = ""
        if live_scores:
            scores_section = "\nLive match status:\n" + "\n".join(f"- {s}" for s in live_scores) + "\n"

        asset_section = ""
        if asset_context:
            asset_section = "\nReal-time market data:\n" + "\n".join(f"- {l}" for l in asset_context) + "\n"

        news_section = ""
        if headlines:
            news_section = "\nRecent news:\n" + "\n".join(f"- {h}" for h in headlines) + "\n"

        return (
            f"Market: {market['name']}\n"
            f"Current YES price: {market['yes_price']:.3f} ({market['yes_price']*100:.1f}%)\n"
            f"Current NO price:  {market['no_price']:.3f} ({market['no_price']*100:.1f}%)\n"
            f"24h volume: ${market['volume_24h']:,.0f}\n"
            f"Liquidity:  ${market['liquidity']:,.0f}\n"
            f"Category tags: {', '.join(str(t) for t in market.get('tags', []))}\n"
            f"{scores_section}"
            f"{asset_section}"
            f"{news_section}\n"
            "Is there a positive edge here? Consider: does the real-time data, live score, "
            "or news suggest the market is mispriced? If you have no strong signal, say edge:false."
        )

    def analyse(self, market: dict, headlines: list[str], live_scores: list[str] = None, asset_context: list[str] = None, shadow: bool = False) -> Optional[dict]:
        """
        Ask Haiku for edge analysis on a market.
        Returns parsed dict {edge, direction, confidence, reasoning, order_type} or None.
        Headlines, live_scores, and asset_context are passed in — the caller fetches them.
        """
        client = self._get_client()
        system = self._build_system_prompt(shadow)
        prompt = self._build_user_prompt(market, headlines, live_scores=live_scores, asset_context=asset_context)

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            # Strip any accidental markdown fences
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            result = json.loads(raw)
            # Validate response fields
            if result.get("direction") not in ("yes", "no"):
                log.warning(f"Haiku returned invalid direction: {result.get('direction')}")
                return None
            conf = result.get("confidence")
            if conf is not None:
                try:
                    conf = max(0.0, min(1.0, float(conf)))
                    result["confidence"] = conf
                except (ValueError, TypeError):
                    result["confidence"] = 0.0
            if "edge" in result and not isinstance(result["edge"], bool):
                result["edge"] = str(result["edge"]).lower() == "true"
            log.info(
                f"🤖 Haiku: '{market['name'][:45]}…' → "
                f"edge={result.get('edge')} conf={result.get('confidence'):.2f} "
                f"dir={result.get('direction')} [{result.get('reasoning','')[:60]}]"
            )
            return result
        except json.JSONDecodeError:
            log.warning(f"Haiku returned non-JSON: {raw[:100]}")
            return None
        except Exception as e:
            log.error(f"Haiku API error: {e}")
            return None

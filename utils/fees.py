"""
Fee calculation and price-scaled parameter adjustment.
"""
from config import CFG


def calc_fee(amount: float, price: float, order_type: str = "taker") -> float:
    """
    Taker fee: parabolic, peaks at 1.8% when price=0.50.
    Maker order: earns a +0.2% rebate (returned as negative fee).
    Formula: fee_rate = 0.018 * 4 * p * (1-p)
    """
    if order_type == "maker":
        return -amount * price * 0.002  # rebate
    rate = 0.018 * 4 * price * (1 - price)
    return amount * price * rate


def price_scaled_params(price: float, base_conf: float, base_amount: float) -> tuple[float, float]:
    """Scale confidence threshold and trade amount based on entry price.

    Low-price markets (high payout) get a lower confidence bar and smaller
    position size -- the asymmetric payout compensates for lower conviction.

    Returns (min_confidence, adjusted_amount).
    """
    lo, hi = 0.15, CFG["MAX_TRADE_PRICE"]
    ratio = max(0.0, min(1.0, (price - lo) / (hi - lo)))
    min_conf = CFG["CONF_FLOOR"] + (base_conf - CFG["CONF_FLOOR"]) * ratio
    size_mult = CFG["SIZE_FLOOR"] + (1.0 - CFG["SIZE_FLOOR"]) * ratio
    return min_conf, round(base_amount * size_mult, 2)

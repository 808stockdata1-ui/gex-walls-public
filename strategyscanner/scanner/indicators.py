"""Price-based indicators. RSI uses Wilder's smoothing (EWMA-14)."""
import math


def rsi_wilder(closes, n: int = 14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def sma(closes, n: int):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def realized_vol(closes, n: int):
    """Annualized close-to-close volatility over the last n returns."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]
    if len(rets) < 5:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def pct_return(closes, n: int):
    if len(closes) < n + 1 or closes[-n - 1] <= 0:
        return None
    return closes[-1] / closes[-n - 1] - 1.0


def avg_volume(vols, n: int = 20):
    v = [x for x in vols[-n:] if x is not None]
    return sum(v) / len(v) if v else None


def ticker_features(bars, live_price=None, live_date=None):
    """Indicator bundle from daily bars. The CBOE (delayed) price is used as the current close:
    appended if it is from a newer session than the last bar, otherwise it replaces the last close."""
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    if live_price and closes:
        if live_date and bars[-1]["date"] < live_date:
            closes = closes + [live_price]
        else:
            closes = closes[:-1] + [live_price]
    price = closes[-1] if closes else None
    f = {
        "price": price,
        "rsi14": rsi_wilder(closes, 14),
        "sma20": sma(closes, 20), "sma50": sma(closes, 50), "sma200": sma(closes, 200),
        "rv20": realized_vol(closes, 20), "rv60": realized_vol(closes, 60),
        "ret5": pct_return(closes, 5), "ret20": pct_return(closes, 20),
        "avg_vol20": avg_volume(vols, 20),
        "hi52": max(closes[-252:]) if closes else None,
    }
    f["above_sma50"] = int(bool(f["sma50"] and price > f["sma50"]))
    f["above_sma200"] = int(bool(f["sma200"] and price > f["sma200"]))
    f["dist_hi52"] = (price / f["hi52"] - 1.0) if f["hi52"] else None
    return f

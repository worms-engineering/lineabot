"""Mock data for local testing: normalized Pinnacle matches.

Both providers return the same normalized shape via get_pinnacle_matches(), so a
single provider-agnostic mock builder is enough. Four tennis matches start in the
next 60 minutes with a Pinnacle Match Winner (H2H) + Total Games line each.
Prices are deterministic, so consecutive scans return the same values (no drop)
unless the caller mutates the stored baseline - handy for testing drop detection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

H2H_MARKET_NAME = "Match Winner"
TOTALS_MARKET_NAME = "Total Games"

# (tournament, home, away, h2h_home, h2h_away, total_line, over, under)
_MATCHES = [
    ("ATP Mock Open", "Jannik Sinner", "Carlos Alcaraz", 1.90, 1.95, 22.5, 1.91, 1.94),
    ("ATP Mock Open", "Novak Djokovic", "Daniil Medvedev", 1.55, 2.45, 23.5, 1.88, 1.97),
    ("WTA Mock Open", "Iga Swiatek", "Aryna Sabalenka", 2.05, 1.80, 21.5, 1.95, 1.90),
    ("ATP Mock Open", "Holger Rune", "Alexander Zverev", 2.30, 1.62, 22.5, 1.90, 1.95),
]


def build_mock_pinnacle_matches(start_epoch: int, end_epoch: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for i, (tour, home, away, hh, ha, line, over, under) in enumerate(_MATCHES):
        st = int((now + timedelta(minutes=10 + i * 12)).timestamp())
        if not (start_epoch < st <= end_epoch):
            continue
        out.append({
            "match_id": f"mock{i}",
            "tournament": tour,
            "player1": home,
            "player2": away,
            "start_epoch": st,
            "selections": [
                {"market_key": "h2h", "market_name": H2H_MARKET_NAME,
                 "outcome": home, "point": None, "label": home, "price": hh},
                {"market_key": "h2h", "market_name": H2H_MARKET_NAME,
                 "outcome": away, "point": None, "label": away, "price": ha},
                {"market_key": "totals", "market_name": TOTALS_MARKET_NAME,
                 "outcome": "Over", "point": line, "label": f"Over {line}", "price": over},
                {"market_key": "totals", "market_name": TOTALS_MARKET_NAME,
                 "outcome": "Under", "point": line, "label": f"Under {line}", "price": under},
            ],
        })
    return out

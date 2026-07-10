"""Mock data for local testing: normalized Pinnacle matches.

All providers return the same normalized shape via get_pinnacle_matches(sport, …),
so one provider-agnostic mock builder is enough. Prices are deterministic, so
consecutive scans return the same values (no drop) unless the caller mutates the
stored baseline - handy for testing drop detection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

H2H_MARKET_NAME = "Match Winner"
TOTALS_MARKET_NAME = "Total"

# per sport: (tournament, home, away, h2h_home, h2h_away, total_line, over, under)
_MATCHES = {
    "tennis": [
        ("ATP Mock Open", "Jannik Sinner", "Carlos Alcaraz", 1.90, 1.95, 22.5, 1.91, 1.94),
        ("ATP Mock Open", "Novak Djokovic", "Daniil Medvedev", 1.55, 2.45, 23.5, 1.88, 1.97),
        ("WTA Mock Open", "Iga Swiatek", "Aryna Sabalenka", 2.05, 1.80, 21.5, 1.95, 1.90),
    ],
    "basketball": [
        ("NBA Summer League", "Lakers", "Celtics", 1.85, 1.98, 181.5, 1.90, 1.92),
        ("WNBA", "Aces", "Liberty", 1.60, 2.35, 165.5, 1.93, 1.90),
        ("EuroBasket", "Spain", "France", 2.10, 1.75, 158.5, 1.91, 1.93),
    ],
}


def build_mock_pinnacle_matches(sport: str, start_epoch: int, end_epoch: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for i, (tour, home, away, hh, ha, line, over, under) in enumerate(_MATCHES.get(sport, [])):
        st = int((now + timedelta(minutes=10 + i * 12)).timestamp())
        if not (start_epoch < st <= end_epoch):
            continue
        out.append({
            "match_id": f"mock-{sport}-{i}",
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

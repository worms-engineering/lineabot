"""Mock The Odds API data for local testing when no valid key is available.

Mirrors the shapes the monitor consumes:

* build_mock_sports()  -> /sports list (one active tennis key)
* build_mock_events()  -> /odds list: tennis events with a Pinnacle bookmaker
                          offering h2h + totals

Four tennis matches start in the next 60 minutes. Prices are deterministic, so
consecutive scans return the same values (no drop) unless the caller mutates the
stored baseline - handy for testing drop detection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# (home, away, h2h_home, h2h_away, total_line, over, under)
_MATCHES = [
    ("Jannik Sinner", "Carlos Alcaraz", 1.90, 1.95, 22.5, 1.91, 1.94),
    ("Novak Djokovic", "Daniil Medvedev", 1.55, 2.45, 23.5, 1.88, 1.97),
    ("Iga Swiatek", "Aryna Sabalenka", 2.05, 1.80, 21.5, 1.95, 1.90),
    ("Holger Rune", "Alexander Zverev", 2.30, 1.62, 22.5, 1.90, 1.95),
]

_SPORT_KEY = "tennis_atp_mock"
_SPORT_TITLE = "ATP Mock Open"


def build_mock_sports() -> list[dict]:
    return [{
        "key": _SPORT_KEY,
        "group": "Tennis",
        "title": _SPORT_TITLE,
        "active": True,
        "has_outrights": False,
    }]


def _iso_in(minutes: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_mock_events() -> list[dict]:
    events: list[dict] = []
    for i, (home, away, hh, ha, line, over, under) in enumerate(_MATCHES):
        events.append({
            "id": f"mockevent{i}",
            "sport_key": _SPORT_KEY,
            "sport_title": _SPORT_TITLE,
            "commence_time": _iso_in(10 + i * 12),  # 10, 22, 34, 46 min from now
            "home_team": home,
            "away_team": away,
            "bookmakers": [{
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": _iso_in(0),
                "markets": [
                    {"key": "h2h", "last_update": _iso_in(0), "outcomes": [
                        {"name": home, "price": hh},
                        {"name": away, "price": ha},
                    ]},
                    {"key": "totals", "last_update": _iso_in(0), "outcomes": [
                        {"name": "Over", "price": over, "point": line},
                        {"name": "Under", "price": under, "point": line},
                    ]},
                ],
            }],
        })
    return events

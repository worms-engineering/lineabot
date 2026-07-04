"""Mock OddsPapi fixtures + odds data for local testing when no valid key is available.

Generates 4 realistic tennis fixtures starting in the next 60 minutes with
Over/Under total games markets across Pinnacle, Bet365, Betfair and Snai.
At least one soft-book quote is deliberately mispriced to trigger a value bet.
"""
from __future__ import annotations

import random
import time


def _rand_price(mu: float, sd: float = 0.05) -> float:
    return round(max(1.02, random.gauss(mu, sd)), 3)


def build_mock_fixtures() -> list[dict]:
    now = int(time.time())
    matches = [
        ("Sinner", "Alcaraz", "ATP Rotterdam", "Netherlands"),
        ("Djokovic", "Medvedev", "ATP Doha", "Qatar"),
        ("Swiatek", "Sabalenka", "WTA Dubai", "UAE"),
        ("Rune", "Zverev", "ATP Marseille", "France"),
    ]
    fixtures = []
    for i, (p1, p2, tournament, category) in enumerate(matches):
        fixtures.append({
            "fixtureId": f"id120000{1000+i}",
            "status": {"live": False, "statusId": 0, "statusName": "Pre-Game"},
            "sport": {"sportId": 12, "sportName": "Tennis"},
            "tournament": {"tournamentId": 500+i, "tournamentName": tournament, "categoryName": category},
            "season": {"seasonId": 1, "seasonName": "2026"},
            "venue": {"venueId": 1, "venueName": "Center Court", "venueLocation": category},
            "startTime": now + (10 + i * 12) * 60,  # 10, 22, 34, 46 minutes from now
            "trueStartTime": None,
            "trueEndTime": None,
            "participants": {
                "participant1Id": 1000 + i * 2,
                "participant1Name": p1, "participant1ShortName": p1, "participant1Abbr": p1[:3].upper(),
                "participant2Id": 1001 + i * 2,
                "participant2Name": p2, "participant2ShortName": p2, "participant2Abbr": p2[:3].upper(),
            },
            "scores": {}, "clock": None, "expectedPeriods": 3, "periodLength": None,
            "externalProviders": {}, "bookmakers": {},
        })
    return fixtures


def build_mock_markets() -> list[dict]:
    """Two Over/Under Games markets per fixture: 22.5 and 23.5."""
    markets = []
    for handicap, market_id_base in ((22.5, 12200), (23.5, 12300)):
        markets.append({
            "marketId": market_id_base,
            "marketLength": 2,
            "sportId": 12,
            "playerProp": False,
            "handicap": handicap,
            "period": "fulltime",
            "marketType": "totals",
            "marketName": "Total Games Over/Under",
            "marketNameShort": "O/U Games",
            "outcomes": [
                {"outcomeId": market_id_base + 1, "outcomeName": "Over"},
                {"outcomeId": market_id_base + 2, "outcomeName": "Under"},
            ],
        })
    return markets


def build_mock_odds(fixture_id: str, bookmakers: list[str]) -> dict:
    """Deterministic-ish odds: pinnacle no-vig ~50/50, soft books add margin
    but 1 in 3 gets a positive edge on one side."""
    random.seed(hash(fixture_id) & 0xFFFF)
    fixtures = {f["fixtureId"]: f for f in build_mock_fixtures()}
    fixture = fixtures.get(fixture_id) or list(fixtures.values())[0]

    odds_by_book: dict[str, dict] = {}
    for market_id_base in (12200, 12300):
        over_id = market_id_base + 1
        under_id = market_id_base + 2

        # Fair prob for over
        p_over_true = random.uniform(0.45, 0.55)
        pin_margin = 1.025
        pin_over = round(1 / (p_over_true * pin_margin), 3)
        pin_under = round(1 / ((1 - p_over_true) * pin_margin), 3)

        for book in bookmakers:
            if book == "pinnacle":
                over_price, under_price = pin_over, pin_under
            else:
                # Soft books usually have larger margin, but ~40% of the time
                # one side of one soft book is deliberately overpriced (public bias)
                margin = random.uniform(1.02, 1.07)
                over_price = round(1 / (p_over_true * margin), 3)
                under_price = round(1 / ((1 - p_over_true) * margin), 3)
                if random.random() < 0.4:
                    side = random.choice(["over", "under"])
                    boost = random.uniform(1.05, 1.12)
                    if side == "over":
                        over_price = round(over_price * boost, 3)
                    else:
                        under_price = round(under_price * boost, 3)
                over_price = max(1.02, over_price)
                under_price = max(1.02, under_price)

            odds_by_book.setdefault(book, {})
            odds_by_book[book][f"{fixture_id}:{book}:{over_id}:0"] = {
                "bookmaker": book, "outcomeId": over_id, "playerId": 0,
                "price": over_price, "active": True, "marketActive": True,
                "mainLine": True, "marketId": market_id_base, "changedAt": int(time.time() * 1000),
            }
            odds_by_book[book][f"{fixture_id}:{book}:{under_id}:0"] = {
                "bookmaker": book, "outcomeId": under_id, "playerId": 0,
                "price": under_price, "active": True, "marketActive": True,
                "mainLine": True, "marketId": market_id_base, "changedAt": int(time.time() * 1000),
            }

    bookmakers_meta = {b: {"bookmaker": b, "hasOdds": True, "staleOdds": False,
                            "suspended": False, "participantsRotated": False,
                            "updatedAt": ""} for b in bookmakers}

    return {**fixture, "odds": odds_by_book, "bookmakers": bookmakers_meta}

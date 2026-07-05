"""Mock OddsPapi v4 data for local testing when no valid key is available.

Mirrors the v4 shapes the monitor consumes:

* build_mock_fixtures(from, to) -> /fixtures list (schedule with names)
* build_mock_odds(book, ids)    -> /odds-by-tournaments list (one bookmaker)
* build_mock_tournaments()      -> /tournaments list
* build_mock_participants()     -> /participants {id: name} map

Four tennis fixtures start in the next 60 minutes with Total Games Over/Under
markets across Pinnacle, Bet365, Betfair and Snai. One soft-book quote is
deliberately mispriced so the monitor produces a value bet.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone


def _fixture_id(tid: int) -> str:
    return f"idmock{tid}"


def _start_epoch(i: int) -> int:
    """Deterministic start time i-th fixture: 10, 22, 34, 46 min from now."""
    dt = datetime.now(timezone.utc) + timedelta(minutes=10 + i * 12)
    return int(dt.timestamp())

# (tournamentId, tournamentName, categoryName, p1_id, p1_name, p2_id, p2_name)
_MATCHES = [
    (5551, "ATP Rotterdam", "ATP", 90001, "Sinner, Jannik", 90002, "Alcaraz, Carlos"),
    (5552, "ATP Doha", "ATP", 90003, "Djokovic, Novak", 90004, "Medvedev, Daniil"),
    (5553, "WTA Dubai", "WTA", 90005, "Swiatek, Iga", 90006, "Sabalenka, Aryna"),
    (5554, "ATP Marseille", "ATP", 90007, "Rune, Holger", 90008, "Zverev, Alexander"),
]

# Two total-games lines per fixture, shared marketId/outcomeId across books.
_LINES = ((22.5, "12200"), (23.5, "12300"))


def build_mock_tournaments() -> list[dict]:
    return [
        {
            "tournamentId": tid,
            "tournamentSlug": name.lower().replace(" ", "-"),
            "tournamentName": name,
            "categorySlug": category.lower(),
            "categoryName": category,
            "futureFixtures": 0,
            "upcomingFixtures": 1,
            "liveFixtures": 0,
        }
        for tid, name, category, *_ in _MATCHES
    ]


def build_mock_participants() -> dict[str, str]:
    names: dict[str, str] = {}
    for _tid, _tn, _cat, p1id, p1, p2id, p2 in _MATCHES:
        names[str(p1id)] = p1
        names[str(p2id)] = p2
    return names


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_mock_fixtures(start_from_epoch: int, start_to_epoch: int) -> list[dict]:
    """Schedule (/fixtures shape) of matches starting within the window."""
    out: list[dict] = []
    for i, (tid, tn, cat, p1id, p1, p2id, p2) in enumerate(_MATCHES):
        st = _start_epoch(i)
        if not (start_from_epoch < st <= start_to_epoch):
            continue
        out.append({
            "fixtureId": _fixture_id(tid),
            "participant1Id": p1id,
            "participant2Id": p2id,
            "participant1Name": p1,
            "participant2Name": p2,
            "sportId": 12,
            "tournamentId": tid,
            "tournamentName": tn,
            "categoryName": cat,
            "statusId": 0,
            "hasOdds": True,
            "startTime": _iso(st),
            "trueStartTime": None,
        })
    return out


def _pinnacle_market(market_id: str, line: float, over_price: float, under_price: float) -> dict:
    over_id, under_id = market_id, str(int(market_id) + 1)
    return {
        "bookmakerMarketId": f"line/mock/{market_id}/totals",
        "marketActive": True,
        "outcomes": {
            over_id: {"players": {"0": {
                "active": True, "price": over_price,
                "bookmakerOutcomeId": f"{line}/over", "mainLine": True,
            }}},
            under_id: {"players": {"0": {
                "active": True, "price": under_price,
                "bookmakerOutcomeId": f"{line}/under", "mainLine": True,
            }}},
        },
    }


def _soft_market(market_id: str, over_price: float, under_price: float) -> dict:
    over_id, under_id = market_id, str(int(market_id) + 1)
    return {
        "bookmakerMarketId": f"line/mock/{market_id}/totals",
        "marketActive": True,
        "outcomes": {
            over_id: {"players": {"0": {"active": True, "price": over_price,
                                        "bookmakerOutcomeId": ""}}},
            under_id: {"players": {"0": {"active": True, "price": under_price,
                                         "bookmakerOutcomeId": ""}}},
        },
    }


# Match-winner (H2H) market: marketId 121, outcomes 121=home, 122=away.
def _h2h_market(home_price: float, away_price: float, is_pinnacle: bool) -> dict:
    return {
        "bookmakerMarketId": "mock/121/0/moneyline" if is_pinnacle else "mock/moneyline",
        "marketActive": True,
        "outcomes": {
            "121": {"players": {"0": {"active": True, "price": home_price,
                                      "bookmakerOutcomeId": "home" if is_pinnacle else "",
                                      "mainLine": True}}},
            "122": {"players": {"0": {"active": True, "price": away_price,
                                      "bookmakerOutcomeId": "away" if is_pinnacle else "",
                                      "mainLine": True}}},
        },
    }


def build_mock_odds(bookmaker: str, tournament_ids: list[int]) -> list[dict]:
    """Fixtures + odds for one bookmaker (v4 shape)."""
    wanted = set(tournament_ids)
    is_pinnacle = bookmaker == "pinnacle"
    fixtures: list[dict] = []

    for i, (tid, _tn, _cat, p1id, _p1, p2id, _p2) in enumerate(_MATCHES):
        if tid not in wanted:
            continue
        random.seed(tid)  # noqa: S311 - deterministic mock, not security-sensitive
        markets: dict[str, dict] = {}

        for line, market_id in _LINES:
            p_over_true = random.uniform(0.45, 0.55)
            pin_margin = 1.025
            pin_over = round(1 / (p_over_true * pin_margin), 3)
            pin_under = round(1 / ((1 - p_over_true) * pin_margin), 3)

            if is_pinnacle:
                markets[market_id] = _pinnacle_market(market_id, line, pin_over, pin_under)
                continue

            margin = random.uniform(1.02, 1.07)
            over_price = round(1 / (p_over_true * margin), 3)
            under_price = round(1 / ((1 - p_over_true) * margin), 3)
            # Deliberately overprice Bet365's Over on the first fixture/line so
            # the monitor surfaces a positive-EV value bet.
            if bookmaker == "bet365" and i == 0 and market_id == "12200":
                over_price = round(over_price * 1.10, 3)
            markets[market_id] = _soft_market(market_id, over_price, under_price)

        # Match winner (H2H) market.
        random.seed(tid + 777)  # noqa: S311 - deterministic mock
        p_home_true = random.uniform(0.4, 0.6)
        if is_pinnacle:
            markets["121"] = _h2h_market(
                round(1 / (p_home_true * 1.025), 3),
                round(1 / ((1 - p_home_true) * 1.025), 3),
                True,
            )
        else:
            hm = random.uniform(1.02, 1.07)
            home_p = round(1 / (p_home_true * hm), 3)
            away_p = round(1 / ((1 - p_home_true) * hm), 3)
            # Overprice Betfair's underdog on the 2nd fixture for a value bet.
            if bookmaker == "betfair" and i == 1:
                away_p = round(away_p * 1.12, 3)
            markets["121"] = _h2h_market(home_p, away_p, False)

        fixtures.append({
            "fixtureId": _fixture_id(tid),
            "participant1Id": p1id,
            "participant2Id": p2id,
            "sportId": 12,
            "tournamentId": tid,
            "seasonId": 1,
            "statusId": 0,
            "hasOdds": True,
            "startTime": _iso(_start_epoch(i)),
            "trueStartTime": None,
            "trueEndTime": None,
            "bookmakerOdds": {
                bookmaker: {
                    "bookmakerIsActive": True,
                    "suspended": False,
                    "markets": markets,
                }
            },
        })
    return fixtures

"""
PropLine as the source of Pinnacle game lines (h2h + main total) for the
classic drop monitor - an alternative to OddsPapi, with its OWN key and daily
quota (PROPLINE_ODDS_API_KEY or the dashboard), separate from the player-props
key so the two never eat each other's requests.

PropLine serves each league as its own sport key, so the monitor's sports map
onto a fixed list of keys below (the OddsPapi tournament whitelists don't
apply). A league costs one request per scan, and only while one of its games
is inside the scan window: the odds response doubles as the league's schedule,
re-read every SCHEDULE_TTL while nothing is in window. Not supported here:
volleyball (one worldwide list with no competition name, so the Italy-only
whitelist can't be applied), Serie C, Coppa Italia (no PropLine key).
"""
from __future__ import annotations

import os
import time

from propline_client import PropLineClient, _iso_epoch, _outcome_decimal

# monitor sport -> [(PropLine sport key, tournament label)]. The football
# labels are the names settlement.py / the Italian-price lookup already map.
GAME_KEYS: dict[str, list[tuple[str, str | None]]] = {
    "tennis": [("tennis", None)],  # every tour; the event carries its tournament
    "basketball": [("basketball_nba", "NBA"), ("basketball_wnba", "WNBA"),
                   ("basketball_euroleague", "EuroLeague")],
    "football": [
        ("soccer_serie_a", "Serie A"), ("soccer_italy_serie_b", "Serie B"),
        ("soccer_epl", "Premier League"), ("soccer_la_liga", "LaLiga"),
        ("soccer_bundesliga", "Bundesliga"), ("soccer_ligue_1", "Ligue 1"),
        ("soccer_eredivisie", "Eredivisie"), ("soccer_primeira_liga", "Liga Portugal"),
        ("soccer_brasileirao", "Brasileiro Serie A"),
        ("soccer_argentina_primera", "Liga Profesional"), ("soccer_liga_mx", "Liga MX"),
        ("soccer_uefa_champions_league", "UEFA Champions League"),
        ("soccer_uefa_europa_league", "UEFA Europa League"),
        ("soccer_uefa_conference_league", "UEFA Conference League"),
        ("soccer_uefa_nations_league", "UEFA Nations League"),
    ],
    "hockey": [("hockey_nhl", "NHL")],
}
SUPPORTED_SPORTS = set(GAME_KEYS)
H2H_MARKET_NAME = "Match Winner"
TOTALS_MARKET_NAME = "Total"
# A league with no game in the window is re-read this often (its odds response
# is also its schedule). Fixtures are listed days ahead, so 3h loses nothing.
SCHEDULE_TTL = int(os.environ.get("PROPLINE_ODDS_SCHEDULE_TTL", str(3 * 3600)))


def _main_total(markets: list[dict]) -> tuple[float, float, float] | None:
    """(line, over, under) of the full-game main total. Team totals are also
    'totals' flagged main (they carry `team`), alt lines are 'alternate'; if a
    game has no main line, the most balanced full-game line stands in."""
    best = None
    for m in markets:
        if (m.get("key") != "totals" or m.get("team") or m.get("period")
                or m.get("suspended_at")):
            continue
        prices = {str(o.get("name") or "").lower(): (_outcome_decimal(o), o.get("point"))
                  for o in m.get("outcomes") or []}
        ov, un = prices.get("over"), prices.get("under")
        if not ov or not un or not ov[0] or not un[0] or ov[1] is None or ov[1] != un[1]:
            continue
        rank = (m.get("line_type") != "main", abs(1 / ov[0] - 1 / un[0]))
        if best is None or rank < best[0]:
            best = (rank, float(ov[1]), ov[0], un[0])
    return best[1:] if best else None


def parse_game_events(events: list, start_epoch: int, end_epoch: int,
                      sport: str, label: str | None) -> list[dict]:
    """PropLine /odds (Pinnacle only) -> monitor matches with the same shape
    as OddsPapiClient.get_pinnacle_matches."""
    out: list[dict] = []
    for ev in events or []:
        if ev.get("live") or ev.get("is_outright"):
            continue
        st = _iso_epoch(ev.get("commence_time"))
        if st is None or not (start_epoch < st <= end_epoch):
            continue
        book = next((b for b in ev.get("bookmakers") or [] if b.get("key") == "pinnacle"), None)
        if not book:
            continue
        p1, p2 = ev.get("home_team") or "1", ev.get("away_team") or "2"
        markets = book.get("markets") or []
        sels: list[dict] = []
        h2h = next((m for m in markets if m.get("key") == "h2h" and not m.get("period")
                    and not m.get("suspended_at")), None)
        if h2h:
            side = {p1: ("home", p1), p2: ("away", p2), "Draw": ("draw", "Pareggio")}
            for o in h2h.get("outcomes") or []:
                hit = side.get(o.get("name"))
                dec = _outcome_decimal(o)
                if hit and dec:
                    sels.append({"market_key": "h2h", "market_name": H2H_MARKET_NAME,
                                 "outcome": hit[0], "point": None, "label": hit[1], "price": dec})
        total = _main_total(markets)
        if total:
            line, over, under = total
            for name, price in (("Over", over), ("Under", under)):
                sels.append({"market_key": "totals", "market_name": TOTALS_MARKET_NAME,
                             "outcome": name, "point": line, "label": f"{name} {line:g}",
                             "price": price})
        if sels:
            out.append({
                "match_id": f"plg-{ev.get('id')}",
                "tournament": label or ev.get("tournament"),
                "player1": p1,
                "player2": p2,
                "start_epoch": st,
                "selections": sels,
            })
    return out


class PropLineOddsClient(PropLineClient):
    name = "propline_odds"

    def __init__(self, api_key: str | None = None):
        super().__init__(api_key=api_key)
        # PropLineClient reads PROPLINE_API_KEY: this client has its own key.
        self.api_key = api_key or os.environ.get("PROPLINE_ODDS_API_KEY")
        # sport_key -> (monotonic time, commence epochs) from the last odds call
        self._game_schedule: dict[str, tuple[float, list[int]]] = {}

    async def get_pinnacle_matches(self, sport: str, start_epoch: int, end_epoch: int,
                                   tournament_filter=None) -> list[dict]:
        if sport not in GAME_KEYS:
            return []
        if not self.api_key:
            raise RuntimeError("key PropLine mercati non impostata (Settings o PROPLINE_ODDS_API_KEY)")
        out: list[dict] = []
        now = time.monotonic()
        for key, label in GAME_KEYS[sport]:
            hit = self._game_schedule.get(key)
            if (hit is not None and now - hit[0] < SCHEDULE_TTL
                    and not any(start_epoch < t <= end_epoch for t in hit[1])):
                continue  # nothing of this league in the window: zero quota
            data = await self._get(f"/sports/{key}/odds",
                                   {"markets": "h2h,totals", "bookmakers": "pinnacle"})
            events = data if isinstance(data, list) else (data or {}).get("data") or []
            self._game_schedule[key] = (now, [t for t in (_iso_epoch(e.get("commence_time"))
                                                          for e in events) if t])
            out.extend(parse_game_events(events, start_epoch, end_epoch, sport, label))
        # a game listed under two keys (e.g. a cup tie) must not alert twice
        seen: set = set()
        return [m for m in out if not (m["match_id"] in seen or seen.add(m["match_id"]))]

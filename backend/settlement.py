"""Alert settlement: grade every sent drop alert as won / lost / push / void.

Runs on its own slow schedule (server.py, SETTLE_MINUTES). An alert becomes
gradable SETTLE_AFTER_SECONDS after its start; if nothing can grade it within
GIVE_UP_SECONDS it is marked "n/d" so it is no longer retried.

Sources (all free):
- PropLine `/scores` (final scores, last 30 days): Pinnacle h2h / totals on
  football, basketball, tennis, hockey, volleyball, and MLB games;
- PropLine `/events/{id}/stats` (box scores): player props (NBA, Serie A,
  Premier, Polymarket NBA props) and the tennis match winner (sets won);
- Polymarket Gamma (market resolution): F1 races / qualifying / sprint / H2H
  and outrights.

Each graded alert gets `result` (won | lost | push | void | half_won |
half_lost | n/d | mixed for multi-line props messages), `result_detail`
(e.g. "2-1", "27 punti"), `result_profit` (units at the alert price, flat
stake 1) and `settled_at`.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import prediction_markets as pmk
import propline_client as plc

logger = logging.getLogger(__name__)

SETTLE_AFTER_SECONDS = 3 * 3600      # games are over ~3h after the start
GIVE_UP_SECONDS = 4 * 86400          # nothing graded it in 4 days -> "n/d"
OUTRIGHT_GIVE_UP_SECONDS = 400 * 86400
LOOKBACK_DAYS = 30                   # PropLine /scores keeps 30 days
PROPLINE_MIN_REMAINING = 100         # never eat the last requests of the day

# Italian props labels (market_name prefix) -> PropLine stat_type. Longest
# prefixes first so "Tiri in porta" isn't read as "Tiri".
PROP_STATS = [
    ("Punti+Rimbalzi+Assist", "points_rebounds_assists"),
    ("Tiri in porta", "shots_on_target"),
    ("Punti", "points"), ("Rimbalzi", "rebounds"), ("Assist", "assists"),
    ("Triple", "threes"), ("Tiri", "shots"),
    ("Marcatore", "goals"),
    ("Doppia doppia", "double_double"),  # goalscorer alerts sent before that market was dropped
]
# Polymarket NBA props use the stat names of their own markets.
_PM_PROP_STATS = {"points": "points", "rebounds": "rebounds", "assists": "assists"}
_FINAL = {"final"}
_VOID_STATUSES = {"postponed", "cancelled"}


def _iso_epoch(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def profit(result: str, price: float | None) -> float:
    p = float(price or 0)
    return {"won": p - 1, "half_won": (p - 1) / 2, "lost": -1.0,
            "half_lost": -0.5}.get(result, 0.0)


def grade_total(total: float, side: str, point: float) -> str:
    """Over/Under incl. Asian quarter lines (2.25 = half on 2 and 2.5)."""
    side = side.lower()
    if abs(point * 4 - round(point * 4)) < 1e-9 and round(point * 4) % 2 == 1:
        a = grade_total(total, side, point - 0.25)
        b = grade_total(total, side, point + 0.25)
        if a == b:
            return a
        pair = {a, b}
        if pair == {"won", "push"}:
            return "half_won"
        if pair == {"lost", "push"}:
            return "half_lost"
        return "lost"
    if abs(total - point) < 1e-9:
        return "push"
    over = total > point
    return "won" if (over if side == "over" else not over) else "lost"


def _side_of(label: str, home: str, away: str, sport: str) -> str | None:
    if str(label).strip().lower() in ("pareggio", "draw", "x"):
        return "draw"
    if plc._same_side(label, home, sport):
        return "home"
    if plc._same_side(label, away, sport):
        return "away"
    return None


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def same_player(a, b) -> bool:
    """Same player across feeds: equal normalized names, or same surname AND
    same first-name initial ("Bronny James" is NOT "LeBron James"; "C. J.
    McCollum" ~ "CJ McCollum"). Suffixes like Jr. are ignored."""
    ta = [t for t in plc._norm(a).split() if t not in _NAME_SUFFIXES]
    tb = [t for t in plc._norm(b).split() if t not in _NAME_SUFFIXES]
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    return ta[-1] == tb[-1] and ta[0][0] == tb[0][0]


def _parse_total_label(label: str) -> tuple[str, float] | None:
    m = re.match(r"^\s*(over|under)\s+(\d+(?:\.\d+)?)", str(label), re.I)
    return (m.group(1).lower(), float(m.group(2))) if m else None


def _prop_from_alert(market_name: str, label: str, sport: str) -> tuple | None:
    """(player, stat_type, side, point) from a props alert line."""
    lab = str(label).strip().lower()
    tot = ({"segna": ("over", 0.5), "non segna": ("under", 0.5),
            "sì": ("over", 0.5), "si": ("over", 0.5), "no": ("under", 0.5)}.get(lab)
           or _parse_total_label(label))
    if not tot:
        return None
    for prefix, stat in PROP_STATS:
        if market_name.startswith(prefix + " "):
            return market_name[len(prefix) + 1:].strip(), stat, tot[0], tot[1]
    return None


class Settler:
    def __init__(self, db, propline: plc.PropLineClient):
        self.db = db
        self.propline = propline
        self._scores: dict[tuple, list] = {}
        self._stats: dict[tuple, dict] = {}
        self._gamma_events: dict[str, list] = {}

    # ---- data access (cached per run) ----------------------------------------
    def _can_call_propline(self) -> bool:
        rem = self.propline.requests_remaining
        return bool(self.propline.api_key) and (rem is None or rem >= PROPLINE_MIN_REMAINING)

    async def _scores_for(self, sport_key: str, days: int) -> list:
        days = max(1, min(LOOKBACK_DAYS, days))
        key = (sport_key, days)
        if key not in self._scores:
            if not self._can_call_propline():
                return []
            data = await self.propline._get(f"/sports/{sport_key}/scores", {"days_from": days})
            self._scores[key] = data if isinstance(data, list) else []
        return self._scores[key]

    async def _stats_for(self, sport_key: str, event_id) -> dict:
        key = (sport_key, str(event_id))
        if key not in self._stats:
            if not self._can_call_propline():
                return {}
            data = await self.propline._get(f"/sports/{sport_key}/events/{event_id}/stats")
            self._stats[key] = data if isinstance(data, dict) else {}
        return self._stats[key]

    async def _find_event(self, sport_key: str, home: str, away: str, start: int,
                          match_sport: str, tol: int):
        age_days = math.ceil((time.time() - start) / 86400) + 1
        for ev in await self._scores_for(sport_key, age_days):
            st = _iso_epoch(ev.get("commence_time"))
            if st is None or abs(st - start) > tol:
                continue
            straight = (plc._same_side(home, ev.get("home_team"), match_sport)
                        and plc._same_side(away, ev.get("away_team"), match_sport))
            swapped = (plc._same_side(home, ev.get("away_team"), match_sport)
                       and plc._same_side(away, ev.get("home_team"), match_sport))
            if straight or swapped:
                return ev, swapped
        return None, False

    # ---- sport key resolution -------------------------------------------------
    @staticmethod
    def sport_key_for(alert: dict) -> str | None:
        sport = alert.get("sport")
        t = str(alert.get("tournament") or "").lower()
        if sport in ("props_nba", "nba_props"):
            return "basketball_nba"
        if sport in plc.SPORT_KEYS:
            return plc.SPORT_KEYS[sport]
        if sport == "football":
            for pat, key in plc.SOCCER_SPORT_KEYS:
                if pat in t:
                    return key
            return None
        if sport == "basketball":
            for pat, key in (("wnba", "basketball_wnba"), ("summer league", None),
                             ("nba", "basketball_nba"), ("euroleague", "basketball_euroleague"),
                             ("eurocup", "basketball_eurocup")):
                if pat in t:
                    return key
            return None
        if sport == "hockey":
            return "hockey_nhl" if "nhl" in t else None
        if sport == "tennis":
            return "tennis"
        if sport == "volley":
            return "volleyball"
        if sport == "mlb":
            return "baseball_mlb"
        return None

    # ---- graders -------------------------------------------------------------
    async def _grade_game(self, alert: dict) -> tuple[str, str] | None:
        """Pinnacle h2h / totals and Polymarket MLB moneyline from final scores."""
        sport = alert.get("sport")
        key = self.sport_key_for(alert)
        if key is None:
            return ("n/d", "competizione non coperta dai risultati")
        start = alert.get("start_epoch")
        match_sport = "tennis" if sport == "tennis" else "football"
        tol = 6 * 3600 if sport == "tennis" else 2 * 3600
        home, away = alert.get("player1"), alert.get("player2")
        ev, swapped = await self._find_event(key, home, away, start, match_sport, tol)
        if ev is None:
            return None
        status = ev.get("status") or ("final" if ev.get("completed") else "")
        if status in _VOID_STATUSES:
            return ("void", f"partita {status}")
        if status in ("retired", "walkover"):
            return ("n/d", f"match {status}")
        if status == "expired":
            return ("n/d", "risultato non disponibile")
        if status not in _FINAL:
            return None
        hs, as_ = ev.get("home_score"), ev.get("away_score")
        if hs is None or as_ is None:
            return None
        # orient the score to the alert's home/away
        h, a = (as_, hs) if swapped else (hs, as_)
        score = f"{h}-{a}"
        label = str(alert.get("label") or "")
        tot = _parse_total_label(label)
        if tot:
            return (grade_total(float(h) + float(a), tot[0], tot[1]),
                    f"{score} (totale {float(h) + float(a):g})")
        side = _side_of(label, home, away, match_sport)
        if side is None:
            return ("n/d", "esito non riconosciuto")
        if sport == "tennis":
            # the score is games won; the winner comes from sets won
            stats = await self._stats_for(key, ev.get("id"))
            sets = {}
            for r in stats.get("stats") or []:
                if r.get("stat_type") in ("sets_w", "sets_won"):
                    sets[plc._norm(r.get("player_name"))] = float(r.get("stat_value") or 0)
            hs_set = next((v for n, v in sets.items() if plc._same_side(n, home, "tennis")), None)
            as_set = next((v for n, v in sets.items() if plc._same_side(n, away, "tennis")), None)
            if hs_set is None or as_set is None or hs_set == as_set:
                return None
            winner = "home" if hs_set > as_set else "away"
            score = f"set {hs_set:g}-{as_set:g}"
        else:
            winner = "home" if h > a else "away" if a > h else "draw"
        return ("won" if side == winner else "lost", score)

    async def _grade_prop_line(self, alert: dict, market_name: str, label: str):
        parsed = _prop_from_alert(market_name, label, alert.get("sport"))
        if parsed is None:
            return ("n/d", "mercato non riconosciuto")
        player, stat, side, point = parsed
        key = self.sport_key_for(alert)
        if key is None:
            return ("n/d", "competizione non coperta")
        ev, _ = await self._find_event(key, alert.get("player1"), alert.get("player2"),
                                       alert.get("start_epoch"), "football", 3 * 3600)
        if ev is None:
            return None
        status = ev.get("status")
        if status in _VOID_STATUSES:
            return ("void", f"partita {status}")
        if status not in _FINAL:
            return None
        stats = await self._stats_for(key, ev.get("id"))
        if stat == "double_double":
            return self._grade_double_double(stats, player, side)
        rows = [r for r in stats.get("stats") or [] if r.get("stat_type") == stat]
        if not rows:
            return None if not stats else ("n/d", "statistica non disponibile")
        # exact name first; else surname + initial, only if unambiguous
        exact = [r for r in rows if plc._norm(r.get("player_name")) == plc._norm(player)]
        close = [r for r in rows if same_player(r.get("player_name"), player)]
        hit = exact or (close if len(close) == 1 else [])
        val = float(hit[0].get("stat_value") or 0) if hit else None
        if val is None:
            return ("void", f"{player} non a referto")
        label_it = {"goals": "gol"}.get(stat) or next((p for p, s in PROP_STATS if s == stat), stat).lower()
        return (grade_total(val, side, point), f"{val:g} {label_it}")

    @staticmethod
    def _player_rows(rows: list, player: str) -> list:
        """The player's box-score rows: exact name first; else surname +
        initial, only if that matches a single player."""
        exact = [r for r in rows if plc._norm(r.get("player_name")) == plc._norm(player)]
        if exact:
            return exact
        names = {r.get("player_name") for r in rows if same_player(r.get("player_name"), player)}
        return [r for r in rows if r.get("player_name") in names] if len(names) == 1 else []

    def _grade_double_double(self, stats: dict, player: str, side: str):
        """Double-double: 10+ in at least two of points, rebounds, assists,
        steals, blocks."""
        cats = ("points", "rebounds", "assists", "steals", "blocks")
        rows = [r for r in stats.get("stats") or [] if r.get("stat_type") in cats]
        if not rows:
            return None if not stats else ("n/d", "statistica non disponibile")
        mine = self._player_rows(rows, player)
        if not mine:
            return ("void", f"{player} non a referto")
        val = {r["stat_type"]: float(r.get("stat_value") or 0) for r in mine}
        n = sum(1 for c in cats if val.get(c, 0) >= 10)
        detail = f"{val.get('points', 0):g} pt · {val.get('rebounds', 0):g} rb · {val.get('assists', 0):g} as"
        return (grade_total(1.0 if n >= 2 else 0.0, side, 0.5), detail)

    async def _gamma_closed(self, tag: str) -> list:
        if tag not in self._gamma_events:
            out: list = []
            client = pmk._get_http()
            for page in range(5):
                r = await client.get(pmk._GAMMA_EVENTS, params={
                    "limit": 100, "offset": page * 100, "closed": "true", "tag_slug": tag,
                    "order": "endDate", "ascending": "false"})
                if r.status_code != 200:
                    break
                ev = r.json()
                if not isinstance(ev, list):
                    break
                out.extend(ev)
                if len(ev) < 100:
                    break
            self._gamma_events[tag] = out
        return self._gamma_events[tag]

    @staticmethod
    def _pm_resolved(m: dict) -> list | None:
        """Final outcome prices of a resolved Polymarket market, else None."""
        try:
            prices = m.get("outcomePrices")
            prices = json.loads(prices) if isinstance(prices, str) else prices
            vals = [float(p) for p in prices]
        except Exception:
            return None
        if not m.get("closed") or not vals or max(vals) < 0.99:
            return None
        return vals

    async def _grade_f1(self, alert: dict):
        gp = str(alert.get("tournament") or "").replace("F1 · ", "").strip()
        mname = alert.get("market_name") or ""
        suffix = next((t[0] for t in pmk.F1_EVENT_MARKETS if t[2] == mname), None)
        if not gp or not suffix:
            return ("n/d", "mercato F1 non riconosciuto")
        label = str(alert.get("label") or "")
        for e in await self._gamma_closed("f1"):
            if str(e.get("title") or "").strip().lower() != f"{gp}: {suffix}".lower():
                continue
            for m in e.get("markets") or []:
                q = str(m.get("question") or "")
                if suffix == "Head-to-Head":
                    if not (plc._same_side(alert.get("player1"), q, "football")
                            and plc._same_side(alert.get("player2"), q, "football")):
                        continue
                    vals = self._pm_resolved(m)
                    if vals is None:
                        return None
                    outs = m.get("outcomes")
                    outs = json.loads(outs) if isinstance(outs, str) else outs
                    winner = outs[vals.index(max(vals))]
                    return ("won" if plc._same_side(winner, label, "tennis") else "lost",
                            f"davanti: {winner}")
                lm = pmk._F1_YES_Q.match(q)
                if not lm or not plc._same_side(lm.group(1), label, "tennis"):
                    continue
                vals = self._pm_resolved(m)
                if vals is None:
                    return None
                return ("won" if vals[0] >= 0.99 else "lost", "Sì" if vals[0] >= 0.99 else "No")
            return None
        return None

    async def _grade_outright(self, alert: dict):
        mid = str(alert.get("match_id") or "")
        m = re.match(r"^pm-outright-(\d+)$", mid)
        if not m:
            return ("n/d", "evento non identificabile")
        r = await pmk._get_http().get(f"{pmk._GAMMA_EVENTS}/{m.group(1)}")
        if r.status_code != 200:
            return None
        ev = r.json()
        for mk in ev.get("markets") or []:
            q = pmk._OUTRIGHT_WIN_Q.match(str(mk.get("question") or ""))
            if q and plc._same_side(q.group(1), alert.get("label"), "tennis"):
                vals = self._pm_resolved(mk)
                if vals is None:
                    return None
                return ("won" if vals[0] >= 0.99 else "lost", "Sì" if vals[0] >= 0.99 else "No")
        return None

    async def grade(self, alert: dict):
        sport = alert.get("sport")
        provider = alert.get("provider")
        if sport == "f1":
            return await self._grade_f1(alert)
        if sport == "outright":
            return await self._grade_outright(alert)
        if provider == "propline" or sport == "nba_props":
            return await self._grade_prop_line(alert, alert.get("market_name") or "",
                                               alert.get("label") or "")
        return await self._grade_game(alert)

    # ---- run -----------------------------------------------------------------
    async def run(self, now: float | None = None) -> dict:
        now = now or time.time()
        since = datetime.fromtimestamp(now - LOOKBACK_DAYS * 86400, tz=timezone.utc).isoformat()
        pending = []
        async for a in self.db.alerts.find({"type": "drop", "result": {"$exists": False},
                                            "created_at": {"$gte": since}}):
            pending.append(a)
        # Alerts older than the results window can never be graded: close them
        # once instead of leaving them "pending" forever.
        stale = await self.db.alerts.update_many(
            {"type": "drop", "result": {"$exists": False}, "created_at": {"$lt": since}},
            {"$set": {"result": "n/d", "result_detail": "fuori dalla finestra dei risultati",
                      "result_profit": 0.0,
                      "settled_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}})
        done = getattr(stale, "modified_count", 0) or 0
        for a in pending:
            start = a.get("start_epoch")
            is_outright = a.get("sport") == "outright"
            if not is_outright and (start is None or now - start < SETTLE_AFTER_SECONDS):
                continue
            give_up = OUTRIGHT_GIVE_UP_SECONDS if is_outright else GIVE_UP_SECONDS
            ref = start or _iso_epoch(a.get("created_at")) or now
            update: dict[str, Any] = {}
            try:
                if a.get("lines"):  # grouped props message: grade each line
                    lines, res = [], []
                    for ln in a["lines"]:
                        g = await self._grade_prop_line(a, ln.get("market_name") or "",
                                                        ln.get("label") or "")
                        if g is None:
                            res = None
                            break
                        lines.append({**ln, "result": g[0], "result_detail": g[1],
                                      "result_profit": round(profit(g[0], ln.get("price")), 3)})
                        res.append(g[0])
                    if res is not None:
                        decided = [r for r in res if r != "n/d"]
                        overall = (decided[0] if decided and len(set(decided)) == 1
                                   else "mixed" if decided else "n/d")
                        update = {"lines": lines, "result": overall,
                                  "result_detail": " · ".join(f"{ln['label']}: {ln['result_detail']}" for ln in lines),
                                  "result_profit": round(sum(ln["result_profit"] for ln in lines), 3)}
                else:
                    g = await self.grade(a)
                    if g is not None:
                        update = {"result": g[0], "result_detail": g[1],
                                  "result_profit": round(profit(g[0], a.get("price")), 3)}
            except Exception as e:
                logger.warning("settle %s failed: %s", a.get("_id"), e)
            if not update and now - ref > give_up:
                update = {"result": "n/d", "result_detail": "esito non trovato", "result_profit": 0.0}
            if update:
                update["settled_at"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
                await self.db.alerts.update_one({"_id": a["_id"]}, {"$set": update})
                done += 1
        return {"pending": len(pending), "settled": done}


def summarize(alerts: list[dict]) -> dict:
    """Totals for the dashboard: per result, hit rate, profit and ROI at the
    alert price (flat stake 1), overall and per source."""
    def bucket(rows):
        c = {"won": 0, "lost": 0, "push": 0, "void": 0, "half_won": 0, "half_lost": 0,
             "mixed": 0, "pending": 0, "n/d": 0}
        prof, staked = 0.0, 0
        for a in rows:
            r = a.get("result")
            if r is None:
                c["pending"] += 1
                continue
            c[r] = c.get(r, 0) + 1
            if r not in ("n/d", "void", "push"):
                prof += float(a.get("result_profit") or 0)
                staked += len(a["lines"]) if a.get("lines") else 1
        decided = c["won"] + c["lost"] + c["half_won"] + c["half_lost"]
        wins = c["won"] + 0.5 * (c["half_won"] + c["half_lost"])
        return {**c, "decided": decided,
                "hit_rate": round(wins / decided, 4) if decided else None,
                "profit": round(prof, 2), "staked": staked,
                "roi": round(prof / staked, 4) if staked else None}

    def source(a):
        if a.get("provider") == "propline" or a.get("sport") == "nba_props":
            return "props"
        if a.get("provider") == "prediction":
            return "polymarket"
        return "pinnacle"

    drops = [a for a in alerts if a.get("type") == "drop"]
    return {"all": bucket(drops),
            "by_source": {s: bucket([a for a in drops if source(a) == s])
                          for s in ("pinnacle", "props", "polymarket")}}

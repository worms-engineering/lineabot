"""Alert settlement: grading rules, player matching, sport keys, a full
Settler.run over fake PropLine scores/box scores, and the dashboard summary."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

from _util import DB, check, iso, report
import settlement as st

# ---- pure helpers ----
check(st.profit("won", 2.5) == 1.5 and st.profit("lost", 2.5) == -1
      and st.profit("half_won", 3.0) == 1.0 and st.profit("half_lost", 3.0) == -0.5
      and st.profit("void", 3.0) == 0 and st.profit("n/d", None) == 0, "profit at flat 1u stake")
check(st.grade_total(3, "over", 2.5) == "won" and st.grade_total(2, "over", 2.5) == "lost"
      and st.grade_total(2, "under", 2.5) == "won" and st.grade_total(2, "over", 2) == "push",
      "plain totals and push")
check(st.grade_total(2, "over", 2.25) == "half_lost" and st.grade_total(3, "over", 2.25) == "won"
      and st.grade_total(3, "over", 2.75) == "half_won" and st.grade_total(2, "under", 2.25) == "half_won",
      "Asian quarter lines -> half results")
check(not st.same_player("Bronny James", "LeBron James"), "Bronny is not LeBron (surname alone)")
check(st.same_player("C.J. McCollum", "CJ McCollum") and st.same_player("Jaren Jackson Jr.", "Jaren Jackson"),
      "initials and suffixes tolerated")
check(st._prop_from_alert("Tiri in porta Mario Rossi", "Over 1.5", "props_seriea")
      == ("Mario Rossi", "shots_on_target", "over", 1.5), "'Tiri in porta' not read as 'Tiri'")
check(st._prop_from_alert("Tiri Mario Rossi", "Over 2.5", "props_epl") == ("Mario Rossi", "shots", "over", 2.5)
      and st._prop_from_alert("Marcatore Mario Rossi", "Segna", "props_seriea") == ("Mario Rossi", "goals", "over", 0.5)
      and st._prop_from_alert("Punti+Rimbalzi+Assist X Y", "Over 30.5", "props_nba")
      == ("X Y", "points_rebounds_assists", "over", 30.5)
      and st._prop_from_alert("Doppia doppia X Y", "Sì", "props_nba") == ("X Y", "double_double", "over", 0.5)
      and st._prop_from_alert("Boh X", "Over 1.5", "props_nba") is None, "props alert labels parsed")
S = st.Settler
check(S.sport_key_for({"sport": "props_nba"}) == "basketball_nba"
      and S.sport_key_for({"sport": "props_unl"}) == "soccer_uefa_nations_league"
      and S.sport_key_for({"sport": "football", "tournament": "Serie B"}) == "soccer_italy_serie_b"
      and S.sport_key_for({"sport": "football", "tournament": "Brasileiro Serie A"}) == "soccer_brasileirao"
      and S.sport_key_for({"sport": "football", "tournament": "Serie C, Group A"}) is None
      and S.sport_key_for({"sport": "basketball", "tournament": "NBA Summer League"}) is None
      and S.sport_key_for({"sport": "hockey", "tournament": "KHL"}) is None
      and S.sport_key_for({"sport": "tennis"}) == "tennis", "sport keys for settlement")


def box(name, **vals):
    return [{"player_name": name, "stat_type": k, "stat_value": v} for k, v in vals.items()]


# ---- full run over fake PropLine data ----
now = time.time()
T0 = int(now) - 5 * 3600          # finished games (started 5h ago)
EVENTS = {
    "soccer_serie_a": [
        {"id": "s1", "home_team": "Inter", "away_team": "Milan", "commence_time": iso(T0),
         "status": "final", "home_score": 2, "away_score": 1},
        # stored by PropLine the other way round
        {"id": "s2", "home_team": "Roma", "away_team": "Lazio", "commence_time": iso(T0),
         "status": "final", "home_score": 0, "away_score": 1},
        {"id": "s3", "home_team": "Genoa", "away_team": "Torino", "commence_time": iso(T0),
         "status": "postponed", "home_score": None, "away_score": None},
    ],
    "basketball_nba": [
        {"id": "n1", "home_team": "Los Angeles Lakers", "away_team": "Boston Celtics",
         "commence_time": iso(T0), "status": "final", "home_score": 110, "away_score": 100},
    ],
    "tennis": [
        {"id": "t1", "home_team": "Jannik Sinner", "away_team": "Carlos Alcaraz",
         "commence_time": iso(T0), "status": "final", "home_score": 13, "away_score": 14},
    ],
}
STATS = {
    "s1": box("Mario Rossi", shots_on_target=1, shots=3) + box("Luca Bianchi", shots_on_target=0, shots=1),
    "n1": box("Bronny James", points=4, rebounds=1, assists=1, steals=0, blocks=0)
          + box("Anthony Davis", points=25, rebounds=12, assists=2, steals=1, blocks=3),
    # the tennis score is games won: Alcaraz has more games, Sinner won 2 sets to 1
    "t1": [{"player_name": "Jannik Sinner", "stat_type": "sets_w", "stat_value": 2},
           {"player_name": "Carlos Alcaraz", "stat_type": "sets_w", "stat_value": 1}],
}


class FakePropLine:
    api_key = "k"
    requests_remaining = None

    async def _get(self, path, params=None):
        parts = path.strip("/").split("/")
        if parts[-1] == "scores":
            return EVENTS.get(parts[1], [])
        if parts[-1] == "stats":
            return {"stats": STATS.get(parts[-2], [])}
        return None


def alert(_id, sport, provider, home, away, market, label, price, start=T0, **kw):
    created = datetime.fromtimestamp(start - 1800, tz=timezone.utc).isoformat()
    return {"_id": _id, "type": "drop", "sport": sport, "provider": provider, "player1": home,
            "player2": away, "market_name": market, "label": label, "price": price,
            "start_epoch": start, "created_at": created, **kw}


async def main():
    db = DB()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    docs = [
        alert("tot", "football", "oddspapi", "Inter", "Milan", "Totale", "Over 2.5", 2.0, tournament="Serie A"),
        alert("h2h", "football", "oddspapi", "Lazio", "Roma", "Match Winner", "Lazio", 2.4, tournament="Serie A"),
        alert("pp", "football", "oddspapi", "Genoa", "Torino", "Match Winner", "Genoa", 2.1, tournament="Serie A"),
        alert("sot", "props_seriea", "propline", "Inter", "Milan", "Tiri in porta Mario Rossi", "Over 0.5", 1.8),
        alert("bronny", "props_nba", "propline", "Los Angeles Lakers", "Boston Celtics",
              "Punti LeBron James", "Over 24.5", 1.9),
        alert("dd", "props_nba", "propline", "Los Angeles Lakers", "Boston Celtics",
              "Doppia doppia Anthony Davis", "Sì", 1.5),
        alert("grp", "props_seriea", "propline", "Inter", "Milan", "Tiri Mario Rossi", "Over 2.5 · Over 3.5", 2.0,
              lines=[{"market_name": "Tiri Mario Rossi", "label": "Over 2.5", "price": 2.0},
                     {"market_name": "Tiri Mario Rossi", "label": "Over 3.5", "price": 4.0}]),
        alert("tennis", "tennis", "oddspapi", "Jannik Sinner", "Carlos Alcaraz", "Match Winner",
              "Jannik Sinner", 2.2),
        alert("recent", "props_seriea", "propline", "Inter", "Milan", "Tiri in porta Mario Rossi",
              "Over 0.5", 1.8, start=int(now) - 3600),
        alert("lost", "props_seriea", "propline", "Udinese", "Verona", "Tiri Mario Rossi", "Over 0.5", 1.5,
              start=int(now) - 5 * 86400),
        {"_id": "stale", "type": "drop", "sport": "props_nba", "provider": "propline",
         "created_at": old, "start_epoch": int(now) - 40 * 86400},
    ]
    for d in docs:
        await db.alerts.insert_one(d)
    res = await st.Settler(db, FakePropLine()).run(now)
    A = {k: v for k, v in db.alerts.d.items()}
    check(A["tot"]["result"] == "won" and A["tot"]["result_detail"].startswith("2-1")
          and A["tot"]["result_profit"] == 1.0, "football total from the final score")
    check(A["h2h"]["result"] == "won" and A["h2h"]["result_detail"] == "1-0",
          "h2h with the event stored home/away swapped")
    check(A["pp"]["result"] == "void", "postponed game -> void")
    check(A["sot"]["result"] == "won" and A["sot"]["result_detail"] == "1 tiri in porta", "props line from box score")
    check(A["bronny"]["result"] == "void", "LeBron alert NOT graded on Bronny's stats -> void")
    check(A["dd"]["result"] == "won" and "25 pt · 12 rb" in A["dd"]["result_detail"], "double-double from box score")
    check(A["grp"]["result"] == "mixed" and [l["result"] for l in A["grp"]["lines"]] == ["won", "lost"]
          and A["grp"]["result_profit"] == 0.0, "grouped alert graded line by line")
    check(A["tennis"]["result"] == "won" and A["tennis"]["result_detail"] == "set 2-1",
          "tennis winner from sets, not games")
    check("result" not in A["recent"], "game started 1h ago: not graded yet")
    check(A["lost"]["result"] == "n/d" and A["lost"]["result_detail"] == "esito non trovato",
          "no result after 4 days -> n/d")
    check(A["stale"]["result"] == "n/d", "alerts older than the results window closed as n/d")
    check(res["settled"] == 10, f"settled count ({res['settled']})")

    # dashboard summary: grouped stake, half results, sources
    sm = st.summarize([
        {"type": "drop", "provider": "propline", "result": "won", "result_profit": 1.0},
        {"type": "drop", "provider": "propline", "result": "mixed", "result_profit": 0.0, "lines": [{}, {}]},
        {"type": "drop", "provider": "oddspapi", "sport": "football", "result": "half_won", "result_profit": 0.5},
        {"type": "drop", "provider": "prediction", "sport": "f1", "result": "lost", "result_profit": -1.0},
        {"type": "drop", "provider": "oddspapi", "sport": "football", "result": "n/d", "result_profit": 0.0},
        {"type": "drop", "provider": "propline"},
        {"type": "whale", "provider": "prediction", "result": "won"},
    ])
    a = sm["all"]
    check(a["won"] == 1 and a["lost"] == 1 and a["half_won"] == 1 and a["pending"] == 1 and a["n/d"] == 1
          and a["staked"] == 5 and a["profit"] == 0.5 and a["hit_rate"] == round(1.5 / 3, 4),
          "summary: grouped lines staked per line, half wins count 0.5, whales ignored")
    check(sm["by_source"]["props"]["won"] == 1 and sm["by_source"]["pinnacle"]["half_won"] == 1
          and sm["by_source"]["polymarket"]["lost"] == 1, "summary by source")
    # current-rules filter
    old_dd = {"created_at": "2026-11-02T00:00:00+00:00", "market_name": "Doppia doppia X"}
    check(not st.under_current_rules(old_dd)
          and st.under_current_rules({**old_dd, "created_at": "2026-11-06T00:00:00+00:00"})
          and st.under_current_rules({"created_at": "2026-11-02T00:00:00+00:00", "market_name": "Punti X"})
          and not st.under_current_rules({"created_at": "2026-11-02T00:00:00+00:00", "market_name": "Punti X",
                                          "lines": [{"market_name": "Doppia doppia X"}]}),
          "results box: old double-doubles (also inside groups) left out")


asyncio.run(main())
report()

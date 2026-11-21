"""PropLine as the Pinnacle game-line source (instead of OddsPapi): parsing,
quota-saving schedule, separate key, scan plan, settings, and a classic
single-price drop alert end to end."""
import asyncio
import copy
import json
import os
import time

from _util import DB, FIXTURES, TG, Prov, Resp, check, iso, report
import monitor as mon
import propline_odds_client as poc
import server

FIX = json.load(open(os.path.join(FIXTURES, "propline_game_lines.json"), encoding="utf-8"))

# ---- parsing (real Pinnacle payloads) ----
sa = poc.parse_game_events(FIX["soccer_serie_a"], 0, 2**31, "football", "Serie A")
lazio = next(m for m in sa if m["player1"] == "Lazio")
labels = {s["label"]: round(s["price"], 3) for s in lazio["selections"]}
check(lazio["tournament"] == "Serie A" and lazio["match_id"].startswith("plg-")
      and {"Lazio", "Lecce", "Pareggio", "Over 3", "Under 3"} == set(labels),
      "football: 1X2 (draw as 'Pareggio') + main total, league label for settlement")
nhl = poc.parse_game_events(FIX["hockey_nhl"], 0, 2**31, "hockey", "NHL")[0]
check({s["label"] for s in nhl["selections"] if s["market_key"] == "totals"} == {"Over 6", "Under 6"},
      "main total = full game (team totals 3.5 / 2.5 also flagged 'main' are ignored)")
te = poc.parse_game_events(FIX["tennis"], 0, 2**31, "tennis", None)
check(len(te) == 2 and all(m["tournament"] for m in te)
      and not any(m["player1"] == "Carlota Martinez Cirez" for m in te),
      "tennis: live match skipped, tournament from the event")
check(poc.parse_game_events(FIX["soccer_serie_a"], 0, 1, "football", "Serie A") == [],
      "games outside the window ignored")
check("volley" not in poc.SUPPORTED_SPORTS and "football" in poc.SUPPORTED_SPORTS,
      "volleyball not served by PropLine (no competition names)")


async def main():
    # ---- quota: a league is only fetched when one of its games is in window
    now = int(time.time())
    calls = []
    events = copy.deepcopy(FIX["hockey_nhl"])
    events[0]["commence_time"] = iso(now + 1800)

    async def get(url, params=None, headers=None):
        calls.append((url, headers.get("X-API-Key")))
        return Resp(events)

    c = poc.PropLineOddsClient(api_key="ODDSKEY")
    c._client.get = get
    out = await c.get_pinnacle_matches("hockey", now, now + 3600)
    check(len(calls) == 1 and calls[0][1] == "ODDSKEY" and len(out) == 1, "first scan reads the league")
    await c.get_pinnacle_matches("hockey", now + 7200, now + 10800)
    check(len(calls) == 1, "nothing in the window -> no request (schedule from the last response)")
    await c.get_pinnacle_matches("hockey", now, now + 3600)
    check(len(calls) == 2, "game in the window -> polled every scan")
    dup = poc.PropLineOddsClient(api_key="K")

    async def get_same(url, params=None, headers=None):
        return Resp(events)  # every basketball key returns the same game

    dup._client.get = get_same
    out = await dup.get_pinnacle_matches("basketball", now, now + 3600)
    check(len(out) == 1, "a game listed under two league keys is returned once")
    try:
        await poc.PropLineOddsClient(api_key="").get_pinnacle_matches("hockey", now, now + 3600)
        check(False, "missing key raises")
    except RuntimeError as e:
        check("key PropLine mercati" in str(e), "missing key -> clear scan error")

    # ---- scan plan / settings
    db = DB()
    m = mon.TennisMonitor(db)
    server.monitor, server.db = m, db
    m.game_line_source = "propline"
    m.tennis_enabled = m.basketball_enabled = m.football_enabled = m.hockey_enabled = m.volley_enabled = True
    m.clients["propline_odds"].api_key = None
    plan = {s: p for s, p, _ in m._scan_plan()}
    check(plan["football"] == plan["volley"] == "oddspapi" and not m.game_lines_via_propline,
          "PropLine selected but no key yet -> OddsPapi keeps covering every sport")
    m.clients["propline_odds"].api_key = "K"
    plan = {s: (p, wl) for s, p, wl in m._scan_plan()}
    check(all(plan[s] == ("propline_odds", None) for s in ("tennis", "basketball", "football", "hockey"))
          and "volley" not in plan, "PropLine source: OddsPapi sports move to PropLine, volley paused")
    m.provider = "theoddsapi"
    check(dict((s, p) for s, p, _ in m._scan_plan())["tennis"] == "theoddsapi",
          "a sport set to The Odds API stays on it")
    m.provider = "oddspapi"
    m.game_line_source = "oddspapi"
    plan = {s: p for s, p, _ in m._scan_plan()}
    check(plan["tennis"] == plan["volley"] == plan["hockey"] == "oddspapi", "OddsPapi source: unchanged")
    s = await server.update_settings(server.SettingsIn(game_line_source="propline",
                                                       propline_odds_api_key="  oddskey1234 "))
    check(s.game_line_source == "propline" and s.propline_odds_key_hint == "…1234"
          and m.clients["propline_odds"].api_key == "oddskey1234"
          and m.clients["propline"].api_key != "oddskey1234", "separate game-lines key, props key untouched")
    m2 = mon.TennisMonitor(db)
    await m2.load_settings()
    check(m2.game_line_source == "propline" and m2.clients["propline_odds"].api_key == "oddskey1234",
          "source and key persist across restart")
    try:
        await server.update_settings(server.SettingsIn(game_line_source="pinnacle"))
        check(False, "unknown source rejected")
    except Exception as e:
        check(getattr(e, "status_code", None) == 400, "unknown source rejected (400)")
    st = await server.get_status()
    check(st.game_line_source == "propline" and st.game_lines_via_propline is True,
          "status exposes the source and whether PropLine is actually serving")

    # ---- end to end: a Pinnacle drop read through PropLine -> classic alert
    ev = copy.deepcopy(FIX["soccer_serie_a"][:1])
    ev[0]["commence_time"] = iso(now + 1800)
    state = {"ev": ev}

    async def get2(url, params=None, headers=None):
        return Resp(state["ev"] if "/soccer_serie_a/" in url else [])

    db3 = DB()
    m3 = mon.TennisMonitor(db3)
    m3.telegram = TG()
    m3.game_line_source = "propline"
    m3.football_drop_threshold = 0.05
    plo = poc.PropLineOddsClient(api_key="K")
    plo._client.get = get2
    m3.clients.update({"oddspapi": Prov(), "theoddsapi": Prov(), "prediction": Prov(), "propline": Prov(),
                       "propline_odds": plo})
    for f in ("tennis", "basketball", "hockey", "volley", "f1", "mlb", "nba_props", "outright", "whale",
              "props_nba", "props_seriea", "props_epl", "props_ucl", "props_uel", "props_unl"):
        setattr(m3, f + "_enabled", False)
    m3.football_enabled = True
    for _ in range(3):
        await m3.scan_once(sports=("football",), update_state=False)
    check(not m3.telegram.sent, "steady prices: no alert")
    h2h = next(mk for mk in state["ev"][0]["bookmakers"][0]["markets"] if mk["key"] == "h2h")
    lazio_o = next(o for o in h2h["outcomes"] if o["name"] == "Lazio")
    lazio_o["price"] = -260                       # 1.50 -> 1.385 (-7.9%)
    await m3.scan_once(sports=("football",), update_state=False)
    sent = [t for t in m3.telegram.sent if "Lazio" in t]
    doc = next((d for d in db3.alerts.d.values() if d.get("type") == "drop"), {})
    check(len(sent) == 1 and doc.get("provider") == "propline_odds" and doc.get("label") == "Lazio"
          and doc.get("tournament") == "Serie A" and "steam_moves" not in doc,
          "Pinnacle drop via PropLine -> one classic drop alert (not the props steam rule)")


asyncio.run(main())
report()

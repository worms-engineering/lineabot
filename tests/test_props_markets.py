"""Props parsing on real PropLine payloads (UEFA cups / Nations League, one
game each in tests/fixtures), the double-double YES-only format, and the
props sports' wiring (fast loop, Over-only gate, toggles, settings)."""
import asyncio
import json
import os

from _util import DB, FIXTURES, check, report
import monitor as mon
import propline_client as plc
import server
import settlement as st

# ---- real payloads ----
for sport, f, mn in (("props_ucl", "propline_ucl.json", 100), ("props_uel", "propline_uel.json", 50),
                     ("props_unl", "propline_unl.json", 100)):
    ev = json.load(open(os.path.join(FIXTURES, f), encoding="utf-8"))
    out = plc.parse_props_events(ev, 0, 2**31, sport=sport)
    ov = [s for m in out for s in m["selections"] if s["outcome"] == "over"]
    check(len(ov) >= mn and all(m["tournament"].startswith(plc.PROP_SPORTS[sport]["label"]) for m in out)
          and any(s["market_name"].startswith("Tiri in porta ") for s in ov)
          and all(len(s["book_prices"]) >= 2 for s in out[0]["selections"]),
          f"{sport}: {len(ov)} Over lines, every line quoted by 2+ books")
    live = plc.parse_props_events([dict(e, live=True) for e in ev], 0, 2**31, sport=sport)
    check(live == [], f"{sport}: live games skipped")

# ---- double-double: FanDuel/DraftKings list only YES (name = player) ----
def o(name, price, point=None, desc="Nikola Jokic"):
    return {"name": name, "description": desc, "price": price, "point": point, "player_id": "nba:1"}

ev = [{"id": "9", "home_team": "Denver Nuggets", "away_team": "Utah Jazz", "commence_time": "2030-01-01T00:00:00Z",
       "bookmakers": [
           {"key": "fanduel", "markets": [
               {"key": "player_points", "outcomes": [o("Over", -110, 24.5), o("Under", -110, 24.5)]},
               {"key": "player_double_double", "outcomes": [o("Nikola Jokic", -200)]}]},
           {"key": "draftkings", "markets": [{"key": "player_double_double", "outcomes": [o("Nikola Jokic", -190)]}]},
           {"key": "hardrock", "markets": [{"key": "player_double_double", "outcomes": [o("Yes", -210), o("No", 160)]}]},
           {"key": "novig", "markets": [{"key": "player_double_double", "outcomes": [o("Over", -205, 0.5),
                                                                                    o("Under", 170, 0.5)]}]}]}]
sels = [s for m in plc.parse_props_events(ev, 0, 2**31, sport="props_nba") for s in m["selections"]]
dd = [s for s in sels if s["market_key"] == "prop:player_double_double:nba:1" and s["outcome"] == "over"]
check(len(dd) == 1 and dd[0]["label"] == "Sì" and dd[0]["point"] == 0.5
      and dd[0]["market_name"] == "Doppia doppia Nikola Jokic"
      and set(dd[0]["book_prices"]) == {"fanduel", "draftkings", "hardrock", "novig"},
      "double-double: YES-only, Yes/No and Over/Under 0.5 formats line up on one 'Sì' line")
check(any(s["market_key"] == "prop:player_points:nba:1" for s in sels), "two-way points line still parsed")
one = [dict(ev[0], bookmakers=ev[0]["bookmakers"][:1])]
check(not any("double" in s["market_key"] for m in plc.parse_props_events(one, 0, 2**31, sport="props_nba")
              for s in m["selections"]), "a double-double quoted by ONE book only is skipped")

# ---- wiring ----
PROPS = ("props_nba", "props_seriea", "props_epl", "props_ucl", "props_uel", "props_unl")
check(all(s in mon.FAST_LOOP_SPORTS and s in mon.PROP_SPORT_KEYS and s in mon.SPORT_META for s in PROPS),
      "every props sport: fast loop, Over-only gate, label")
check(all(st.Settler.sport_key_for({"sport": s}) == plc.SPORT_KEYS[s] for s in PROPS),
      "every props sport resolves for settlement")
check("props_uecl" not in plc.SPORT_KEYS, "Conference League deliberately absent")


async def main():
    db = DB()
    m = mon.TennisMonitor(db)
    server.monitor, server.db = m, db
    check(all(s in [x for x, _, _ in m._scan_plan()] for s in PROPS), "all props sports on by default")
    s = await server.update_settings(server.SettingsIn(props_unl_enabled=False, props_uel_enabled=False))
    check(s.props_unl_enabled is False and s.props_uel_enabled is False and s.props_ucl_enabled is True
          and "props_unl" not in [x for x, _, _ in m._scan_plan()], "toggles off via settings")
    m2 = mon.TennisMonitor(db)
    await m2.load_settings()
    check(m2.props_unl_enabled is False and m2.props_ucl_enabled is True, "toggles persist across restart")
    status = await server.get_status()
    check(status.props_unl_enabled is False and status.props_epl_enabled is True, "status exposes the toggles")


asyncio.run(main())
report()

"""PropLine props steam rule: first-mover leader, Bovada never alone, stale
outliers, anti-spam (new low only), late-listed books, Over only, and the
double-double two-leader requirement."""
import asyncio
import time

from _util import DB, Resp, check, iso, props_only_monitor, report
import monitor as mon
import propline_client as plc

MARKET = {"k": "player_points"}


def slate(st, prices):
    """prices: {book: (over_american, under_american)} for LeBron 24.5."""
    bms = []
    for bk, (ov, un) in prices.items():
        bms.append({"key": bk, "markets": [{"key": MARKET["k"], "outcomes": [
            {"name": "Over", "description": "LeBron James", "price": ov, "point": 24.5},
            {"name": "Under", "description": "LeBron James", "price": un, "point": 24.5}]}]})
    return [{"id": "1", "home_team": "Los Angeles Lakers", "away_team": "Golden State Warriors",
             "commence_time": iso(st), "live": False, "bookmakers": bms}]


async def main():
    st = int(time.time()) + 1800
    state = {"p": {"fanduel": (-110, -110), "draftkings": (-110, -110), "betmgm": (-110, -110)}}

    async def get(url, params=None, headers=None):
        if url.endswith("/events"):
            return Resp([{"id": "1", "commence_time": iso(st)}])
        return Resp(slate(st, state["p"]))

    pl = plc.PropLineClient(api_key="K")
    pl._client.get = get
    db = DB()
    m = props_only_monitor(mon, db, pl)
    sp = ("props_nba",)

    async def scan(mm=m):
        pl._last_fetch.clear()
        await mm.scan_once(sports=sp, update_state=False)

    sent = lambda: [t for t in m.telegram.sent if "LeBron" in t and "Over 24.5" in t]

    for _ in range(3):
        await scan()
    check(not m.telegram.sent, "baseline: no alert")
    state["p"]["bovada"] = (-110, -110)
    for _ in range(3):
        await scan()
    state["p"]["bovada"] = (-150, 120)            # 1.91 -> 1.67 (-13%), others flat
    await scan()
    check(not sent(), "Bovada moving alone -> NO alert")
    state["p"]["bovada"] = (-110, -110)
    await scan()
    state["p"]["fanduel"] = (-110, -135)          # only the UNDER shortens
    await scan()
    check(not m.telegram.sent, "an Under move never alerts on props (Over only)")
    state["p"]["fanduel"] = (-110, -110)
    await scan()
    state["p"]["fanduel"] = (-135, 105)           # FanDuel alone: Over 1.909 -> 1.741 (-8.8%)
    await scan()
    a = sent()
    check(len(a) == 1 and "STEAM PROPS (1 leader)" in a[0] and "FanDuel★ 1.91→<b>1.74</b>" in a[0]
          and "Ancora fermi" in a[0] and "BetMGM <b>1.91</b>" in a[0]
          and "(ancora fermo)" in a[0],
          "ONE leader ahead of the market (first mover) -> alert, consensus shown as still")
    doc = next(d for d in db.alerts.d.values() if d.get("steam_moves"))
    check(doc["steam_moves"] == [{"book": "FanDuel", "from": 1.909, "to": 1.741, "move": 0.088, "leader": True}]
          and {f["book"] for f in doc["steam_flat"]} == {"DraftKings", "BetMGM", "Bovada"},
          "alert doc stores which books moved and which held")
    state["p"]["draftkings"] = (-130, 100)        # DK joins: 1.909 -> 1.769 (-7.3%)
    await scan()
    check(len(sent()) == 1, "second leader joining at a higher price -> no repeat")
    await scan()
    await scan()
    check(len(sent()) == 1, "same books next scans -> no repeat")
    state["p"]["betmgm"] = (-125, -105)
    await scan()
    check(len(sent()) == 1, "third book joins at the same level -> no repeat (rule A)")
    state["p"] = {"fanduel": (-110, -110), "draftkings": (-110, -110), "betmgm": (-110, -110)}
    await scan()
    state["p"]["draftkings"] = (-130, 100)
    state["p"]["betmgm"] = (-130, 100)
    await scan()
    check(len(sent()) == 1, "move fades and returns to the same level -> no repeat (rule A)")
    state["p"] = {"fanduel": (-110, -110), "draftkings": (-110, -110), "betmgm": (-110, -110),
                  "bovada": (-110, -110)}
    await scan()
    n0 = len(sent())
    state["p"]["betmgm"] = (-160, 130)
    await scan()
    await scan()
    check(len(sent()) == n0, "a soft (non-leader) book moving alone -> no alert")
    state["p"] = {"fanduel": (-110, -110), "draftkings": (-110, -110), "betmgm": (-110, -110)}
    await scan()
    n = len(sent())
    state["p"]["fanduel"] = (-130, 100)
    state["p"]["bovada"] = (-150, 120)            # bovada new, already "low"
    await scan()
    check(len(sent()) == n, "late-listed book's first price is its baseline, not a move")

    # a leader that was a stale outlier from the start (DK opens at 2.10)
    m2 = props_only_monitor(mon, DB(), pl)
    state["p"] = {"fanduel": (-110, -110), "draftkings": (110, -130), "betmgm": (-110, -110),
                  "bovada": (-110, -110)}
    for _ in range(3):
        await scan(m2)
    state["p"]["draftkings"] = (-125, 105)        # 2.10 -> 1.80, barely below the market
    await scan(m2)
    check(not m2.telegram.sent, "stale leader falling back to (slightly past) the market -> no alert")
    state["p"]["draftkings"] = (-160, 130)
    await scan(m2)
    check(not m2.telegram.sent, "...and still none: it was never in line before moving")

    # double-double: a lone leader never fires, two do
    MARKET["k"] = "player_double_double"
    m3 = props_only_monitor(mon, DB(), pl)
    state["p"] = {"fanduel": (-110, -110), "draftkings": (-110, -110), "betmgm": (-110, -110),
                  "bovada": (-110, -110)}
    for _ in range(3):
        await scan(m3)
    state["p"]["fanduel"] = (-135, 105)
    await scan(m3)
    check(not m3.telegram.sent, "double-double: one leader alone -> no alert")
    state["p"]["draftkings"] = (-135, 105)
    await scan(m3)
    check(len(m3.telegram.sent) == 1 and "2 leader" in m3.telegram.sent[0],
          "double-double: two leaders -> alert")


asyncio.run(main())
report()

"""Football competition whitelist (OddsPapi names), Italian-price league
mappings, and the alerts / results API projections."""
import asyncio

from _util import DB, check, report
import monitor as mon
import oddspapi_client as oc
import propline_client as plc
import server
import theoddsapi_client as tc

WL = mon.FOOTBALL_WHITELIST_ODDSPAPI
ok = lambda cat, name: oc._matches_whitelist({"categoryName": cat, "tournamentName": name}, WL)
keep = [("Italy", "Serie A"), ("Italy", "Serie B"), ("Italy", "Serie B, Playoffs"),
        ("Italy", "Serie C, Group A"), ("Italy", "Serie C, Girone B"), ("Italy", "Serie C, Playoffs"),
        ("Italy", "Coppa Italia"), ("England", "Premier League"), ("Netherlands", "Eredivisie"),
        ("International Clubs", "UEFA Champions League"), ("International Clubs", "UEFA Europa League"),
        ("International Clubs", "UEFA Conference League"),
        ("International Clubs", "UEFA Champions League Qualification"),
        ("International", "UEFA Nations League")]
drop = [("International Clubs", "Club Friendly Games"), ("International Youth", "U19 Friendly Games"),
        ("International", "Int. Friendly Games"), ("International Clubs", "AFC Champions League Elite"),
        ("International Clubs", "CAF Champions League"), ("International Clubs", "UEFA Champions League Women"),
        ("International Clubs", "UEFA Youth League"), ("Italy", "Coppa Italia Serie C"),
        ("Italy", "Serie D, Girone A"), ("Italy", "Serie B Women"), ("Italy", "Serie A Women"),
        ("Brazil", "Brasileiro Serie B"), ("Italy", "Primavera 1"), ("Germany", "2. Bundesliga"),
        ("Netherlands", "Eredivisie SRL")]
check(all(ok(c, n) for c, n in keep), "kept: top flights, Serie B/C, UEFA cups incl. qualifying, Nations League")
check(not any(ok(c, n) for c, n in drop), "dropped: friendlies, AFC/CAF, women/youth, lower tiers, cups")
check(oc._matches_whitelist({"categoryName": "International", "tournamentName": "CEV Champions League, Women"},
                            mon.VOLLEY_WHITELIST_ODDSPAPI), "volley women's CL (exact entry) unaffected")
pkey = lambda t: next((k for p, k in plc.SOCCER_SPORT_KEYS if p in t.lower()), None)
tkey = lambda t: next((k for p, k in tc.LEAGUE_SPORT_KEYS if p in t.lower()), None)
check(pkey("Serie B") == "soccer_italy_serie_b" and pkey("Serie A") == "soccer_serie_a"
      and pkey("Brasileiro Serie A") == "soccer_brasileirao" and pkey("Serie C, Group A") is None,
      "PropLine league keys (Brazil before generic Serie A)")
check(tkey("Serie B") == "soccer_italy_serie_b" and "soccer_italy_serie_b" in tc.FOOTBALL_LEAGUE_KEYS,
      "The Odds API: Serie B mapped")


class Cur:
    def sort(self, *a):
        return self

    async def to_list(self, n):
        return []


class Spy:
    def find(self, q, proj):
        self.q, self.proj = q, proj
        return Cur()


async def main():
    spy = Spy()
    server.db = type("D", (), {"alerts": spy})()
    await server.list_alerts()
    check(all(spy.proj.get(k) == 0 for k in ("message", "steam_moves", "steam_flat", "lines.steam_moves",
                                             "telegram_response")), "alerts: dashboard payload stays lean")
    await server.list_alerts(detail=True)
    check("message" not in spy.proj and "steam_moves" not in spy.proj and spy.proj.get("telegram_response") == 0,
          "alerts?detail=true: text and moves included, raw Telegram response never")
    check(mon._steam_fields({"market_name": "x"}) == {}, "non-steam alerts store no book moves")

    db = DB()
    server.db = db
    rows = [
        {"_id": 1, "type": "drop", "provider": "propline", "created_at": "2026-10-29T00:00:00+00:00",
         "market_name": "Punti X", "result": "lost", "result_profit": -1.0},
        {"_id": 2, "type": "drop", "provider": "propline", "created_at": "2026-11-01T00:00:00+00:00",
         "market_name": "Punti X", "result": "won", "result_profit": 0.9},
        {"_id": 3, "type": "drop", "provider": "propline", "created_at": "2026-11-01T00:00:00+00:00",
         "market_name": "Doppia doppia X", "result": "lost", "result_profit": -1.0},
    ]
    for r in rows:
        await db.alerts.insert_one(r)
    s = await server.results_summary()
    check(s["since"] == "2026-10-30T00:40:00+00:00" and s["all"]["won"] == 1 and s["all"]["lost"] == 0,
          "results box: only alerts under the current rules")
    s = await server.results_summary(days=3650)
    check(s["all"]["won"] == 1 and s["all"]["lost"] == 2, "results ?days=N: rolling window, every alert")


asyncio.run(main())
report()

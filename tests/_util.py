"""Shared fakes for the backend test scripts. Run them all with
`python tests/run_all.py` (needs backend/requirements.txt installed; no
network, no MongoDB, no Telegram)."""
import copy
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("MONGO_URL", "mongodb://test")
os.environ.setdefault("DB_NAME", "test")

FAILED: list[str] = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def report():
    print("ALL PASS" if not FAILED else "FAILED: " + str(FAILED))
    sys.exit(1 if FAILED else 0)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ---- in-memory MongoDB (only what the monitor / server / settler use) ----

def _get(doc, path):
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return None, False
        doc = doc[part]
    return doc, True


def _setpath(doc, path, val):
    parts = path.split(".")
    for p in parts[:-1]:
        doc = doc.setdefault(p, {})
    doc[parts[-1]] = val


def _matches(doc, q):
    for k, cond in q.items():
        val, present = _get(doc, k)
        if isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$in" and val not in arg:
                    return False
                if op == "$exists" and present != bool(arg):
                    return False
                if op == "$gte" and not (present and val >= arg):
                    return False
                if op == "$lt" and not (present and val < arg):
                    return False
        elif val != cond:
            return False
    return True


def _project(doc, proj):
    doc = copy.deepcopy(doc)
    for k, v in (proj or {}).items():
        if v == 0:
            parts = k.split(".")
            if len(parts) == 1:
                doc.pop(k, None)
            elif isinstance(doc.get(parts[0]), list):
                for item in doc[parts[0]]:
                    item.pop(parts[1], None)
    return doc


class Cursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda d: d.get(key) or "", reverse=direction < 0)
        return self

    async def to_list(self, n):
        return self.docs[:n]

    def __aiter__(self):
        self._it = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class Result:
    def __init__(self, n):
        self.modified_count = n


class Coll:
    def __init__(self):
        self.d: dict = {}

    async def find_one(self, q, proj=None):
        doc = next((d for d in self.d.values() if _matches(d, q)), None)
        return _project(doc, proj) if doc is not None else None

    def find(self, q=None, proj=None):
        return Cursor(_project(d, proj) for d in self.d.values() if _matches(d, q or {}))

    def _apply(self, doc, upd):
        for k, v in (upd.get("$set") or {}).items():
            _setpath(doc, k, copy.deepcopy(v))
        for k in (upd.get("$unset") or {}):
            doc.pop(k, None)

    async def update_one(self, q, upd, upsert=False):
        doc = next((d for d in self.d.values() if _matches(d, q)), None)
        if doc is None:
            if not upsert and "_id" not in q:
                return Result(0)
            doc = {"_id": q.get("_id")}
            self.d[doc["_id"]] = doc
        self._apply(doc, upd)
        return Result(1)

    async def update_many(self, q, upd):
        docs = [d for d in self.d.values() if _matches(d, q)]
        for doc in docs:
            self._apply(doc, upd)
        return Result(len(docs))

    async def bulk_write(self, ops, ordered=True):
        for op in ops:
            await self.update_one(op._filter, op._doc, upsert=True)

    async def insert_one(self, doc):
        self.d[doc["_id"]] = copy.deepcopy(doc)

    async def delete_many(self, q):
        pass


class DB:
    def __init__(self):
        for n in ("settings", "alerts", "line_state", "snapshots", "scans"):
            setattr(self, n, Coll())


# ---- other fakes ----

class TG:
    token = chat_id = "x"

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, text):
        self.sent.append(text)
        return {"ok": True, "result": {"message_id": len(self.sent)}}

    async def edit_message(self, *a):
        return {"ok": True}

    async def close(self):
        pass


class Prov:
    """Stand-in for the odds clients a test doesn't exercise."""
    use_mock = False
    requests_remaining = None
    quota_exhausted = False
    ip_blocked = False
    api_key = "k"

    async def get_pinnacle_matches(self, sport, ws, we, wl):
        return []

    async def close(self):
        pass


class Resp:
    """httpx.Response stand-in for PropLineClient._client.get."""
    def __init__(self, js):
        self.status_code, self._js, self.text = 200, js, ""
        self.headers = {"X-Daily-Remaining": "900", "X-Daily-Limit": "1000"}

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


SPORT_FLAGS = ("outright", "whale", "nba_props", "f1", "mlb", "tennis", "basketball",
               "football", "hockey", "volley")


def props_only_monitor(mon, db, propline, threshold=0.04):
    """A monitor that only scans the PropLine props, with a fake Telegram."""
    m = mon.TennisMonitor(db)
    m.telegram = TG()
    m.props_drop_threshold = threshold
    m.clients = {"oddspapi": Prov(), "theoddsapi": Prov(), "prediction": Prov(),
                 "propline": propline}
    for f in SPORT_FLAGS:
        setattr(m, f + "_enabled", False)
    return m

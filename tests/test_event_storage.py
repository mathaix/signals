"""save_event against an in-memory stand-in for the supabase client (PostgREST semantics we rely on)."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import storage
from eventpage import extract
from htmlparse import parse

FIXTURES = Path(__file__).parent / "fixtures"
UNIQUE = {"events": ("event_key",), "people": ("person_key",), "companies": ("domain",),
          "appearances": ("event_id", "company_id", "person_id", "role")}
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


class Result:
    def __init__(self, data):
        self.data = data


class FakeDB:
    def __init__(self):
        self.t = {n: [] for n in ("events", "people", "companies", "scrapes", "appearances")}
        self.ids = {n: 0 for n in self.t}

    def table(self, name):
        return Query(self, name)

    def add(self, name, row):
        self.ids[name] += 1
        row = {"id": self.ids[name], "parent_id": None, "merged_into": None, "last_scraped_at": None, **row}
        if name == "events":
            row["title_key"] = re.sub(r"[^a-z0-9]+", "", (row.get("title") or "").lower())
        self.t[name].append(row)
        return row


class Query:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.op, self.n = db, name, [], "select", None

    def select(self, cols):  # like supabase-py, every select() starts a fresh builder
        q = Query(self.db, self.name)
        q.cols = cols.split(",")
        return q

    def eq(self, k, v):
        self.filters.append(lambda r: r.get(k) == v)
        return self

    def in_(self, k, vs):
        self.filters.append(lambda r: r.get(k) in vs)
        return self

    def order(self, k):
        return self

    def limit(self, n):
        self.n = n
        return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows
        return self

    def upsert(self, rows, on_conflict, ignore_duplicates=False):
        self.op, self.payload, self.ignore = "upsert", rows if isinstance(rows, list) else [rows], ignore_duplicates
        assert tuple(on_conflict.split(",")) == UNIQUE[self.name]
        keys = [tuple(r.get(c) for c in UNIQUE[self.name]) for r in self.payload]
        assert len(set(keys)) == len(keys), "Postgres rejects the same conflict key twice in one upsert"
        return self

    def execute(self):
        db, rows = self.db.t[self.name], None
        if self.op == "insert":
            return Result([dict(db_row) for db_row in (self.db.add(self.name, r) for r in self.payload)])
        if self.op == "upsert":
            out = []
            for r in self.payload:
                assert "first_seen_at" not in r and "confirmed" not in r
                key = UNIQUE[self.name]
                have = next((x for x in db if all(x.get(k) == r.get(k) for k in key)), None)
                if have is None:
                    have = self.db.add(self.name, r)
                elif not self.ignore:
                    have.update(r)
                out.append(dict(have))
            return Result(out)
        rows = [r for r in db if all(f(r) for f in self.filters)][: self.n]
        return Result([{c: r.get(c) for c in self.cols} for r in rows])


def seed_scrape(db, parent_id=None, batch_id="b1"):
    return db.add("scrapes", {"url": "https://saasexpo.example/2026", "status": "queued", "batch_id": batch_id, "parent_id": parent_id})["id"]


def result(name="event_sponsors.html", base="https://saasexpo.example/2026"):
    return extract(parse((FIXTURES / name).read_text(), base))


def appearances(db):
    names = {c["id"]: c["domain"] for c in db.t["companies"]}
    return sorted((names.get(a["company_id"]), a["role"]) for a in db.t["appearances"] if a["company_id"])


def test_save_event_writes_event_companies_appearances_and_queues_children():
    db = FakeDB()
    sid = seed_scrape(db)
    out = storage.save_event(sid, result(), client=db, now=NOW)
    ev = db.t["events"][0]
    assert (ev["event_key"], ev["event_date"], ev["ends_on"], ev["location"], ev["last_scrape_id"]) == \
        ("saasexpo.example/2026", "2026-11-10", "2026-11-11", "Moscone Center, San Francisco, CA", sid)
    assert appearances(db) == sorted([("acme.com", "sponsor"), ("initech.com", "sponsor"), ("globex.com", "exhibitor"),
                                      ("acme.com", "exhibitor"), ("umbrella.co.uk", "exhibitor"), ("hooli.io", "partner"),
                                      ("stark.com", "listed")])
    evidence = {(a["role"], a["evidence"]) for a in db.t["appearances"]}
    assert ("sponsor", "Gold Sponsors") in evidence and ("listed", "About") in evidence
    # one child per distinct company, in page order, same batch, parent = the event scrape
    kids = [r for r in db.t["scrapes"] if r["parent_id"] == sid]
    assert [k["url"] for k in kids] == [f"https://{d}" for d in ("acme.com", "initech.com", "globex.com", "umbrella.co.uk", "hooli.io", "stark.com")]
    assert {(k["batch_id"], k["status"]) for k in kids} == {("b1", "queued")}
    assert [c["id"] for c in out["children"]] == [k["id"] for k in kids] and out["overflow"] == []
    assert {c["domain"]: c["name"] for c in db.t["companies"]}["initech.com"] == "Initech"


def test_rescrape_adds_no_duplicates_and_still_listed_tracks_removed_sponsor():
    db = FakeDB()
    first = seed_scrape(db)
    storage.save_event(first, result(), client=db, now=NOW)
    n_app, n_ev, n_co = len(db.t["appearances"]), len(db.t["events"]), len(db.t["companies"])
    second = seed_scrape(db, batch_id="b2")
    storage.save_event(second, result(), client=db, now=NOW)
    assert (len(db.t["appearances"]), len(db.t["events"]), len(db.t["companies"])) == (n_app, n_ev, n_co)
    assert {a["last_scrape_id"] for a in db.t["appearances"]} == {second}

    # a third scrape where Globex's exhibitor listing is gone: only that row keeps the older scrape id
    gone = result()
    gone["companies"] = [c for c in gone["companies"] if c["domain"] != "globex.com"]
    third = seed_scrape(db, batch_id="b3")
    storage.save_event(third, gone, client=db, now=NOW)
    assert len(db.t["appearances"]) == n_app
    latest = {a["last_scrape_id"] for a in db.t["appearances"]}
    stale = [a for a in db.t["appearances"] if a["last_scrape_id"] < third]  # still_listed = last_scrape_id >= latest ok scrape
    assert latest == {second, third} and len(stale) == 1
    assert db.t["companies"][stale[0]["company_id"] - 1]["domain"] == "globex.com"


def test_rescrape_does_not_requeue_children_already_queued():
    db = FakeDB()
    storage.save_event(seed_scrape(db), result(), client=db, now=NOW)
    n = len(db.t["scrapes"])
    out = storage.save_event(seed_scrape(db, batch_id="b2"), result(), client=db, now=NOW)
    assert len(db.t["scrapes"]) == n + 1 and out["children"] == []  # just the new event scrape itself


def test_companies_scraped_within_30_days_are_not_queued_older_or_never_are():
    db = FakeDB()
    db.add("companies", {"domain": "acme.com", "last_scraped_at": (NOW - timedelta(days=29)).isoformat()})
    db.add("companies", {"domain": "initech.com", "last_scraped_at": (NOW - timedelta(days=31)).isoformat()})
    sid = seed_scrape(db)
    out = storage.save_event(sid, result(), client=db, now=NOW)
    urls = [c["url"] for c in out["children"]]
    assert "https://acme.com" not in urls and "https://initech.com" in urls and "https://globex.com" in urls


def test_only_depth_zero_scrapes_fan_out_but_children_still_record_appearances():
    db = FakeDB()
    parent = seed_scrape(db)
    child = seed_scrape(db, parent_id=parent)
    out = storage.save_event(child, result(), client=db, now=NOW)
    assert out["children"] == [] and out["overflow"] == []
    assert [r for r in db.t["scrapes"] if r["parent_id"] == child] == []
    assert len(db.t["appearances"]) == 7


def test_fan_out_cap_spawns_200_and_overflow_stays_queued():
    db = FakeDB()
    res = result()
    res["companies"] = [{"domain": f"c{i:03}.com", "url": f"https://c{i:03}.com", "name": None, "role": "listed", "evidence": None}
                        for i in range(230)]
    sid = seed_scrape(db)
    out = storage.save_event(sid, res, client=db, now=NOW)
    assert [c["url"] for c in out["children"]] == [f"https://c{i:03}.com" for i in range(200)]
    assert [c["url"] for c in out["overflow"]] == [f"https://c{i:03}.com" for i in range(200, 230)]
    assert sum(1 for r in db.t["scrapes"] if r["parent_id"] == sid and r["status"] == "queued") == 230


def test_merged_company_gets_the_appearance_but_no_child_scrape():
    db = FakeDB()
    keep = db.add("companies", {"domain": "initech.com"})["id"]
    db.add("companies", {"domain": "acme.com", "merged_into": keep})
    out = storage.save_event(seed_scrape(db), result(), client=db, now=NOW)
    sponsor_ids = {a["company_id"] for a in db.t["appearances"] if a["role"] == "sponsor"}
    assert sponsor_ids == {keep}  # acme.com (merged) and initech.com collapse onto the kept row
    assert "https://acme.com" not in [c["url"] for c in out["children"]]


def test_people_upserted_by_key_and_appearances_carry_title_and_employer_as_printed():
    db = FakeDB()
    res = result("event_luma.html", "https://lu.ma/founders")
    res["companies"].append({"domain": "initech.com", "url": "https://initech.com", "name": "Initech", "role": "sponsor", "evidence": "Sponsors"})
    storage.save_event(seed_scrape(db), res, client=db, now=NOW)
    storage.save_event(seed_scrape(db, batch_id="b2"), res, client=db, now=NOW)
    keys = sorted(p["person_key"] for p in db.t["people"])
    assert len(keys) == 2 and keys[0] == "li:alice-smith" and keys[1].startswith("nm:bobjones@event:")
    initech = next(c for c in db.t["companies"] if c["domain"] == "initech.com")
    alice = next(a for a in db.t["appearances"] if a["person_id"] and a["job_title"] == "CTO")
    assert (alice["company_name"], alice["person_company_id"], alice["role"]) == ("Initech", initech["id"], "speaker")
    assert len([a for a in db.t["appearances"] if a["person_id"]]) == 2


def test_cross_host_event_reuses_existing_row_by_date_and_title():
    db = FakeDB()
    storage.save_event(seed_scrape(db), result(), client=db, now=NOW)
    other = result(base="https://www.eventbrite.example/e/9")
    other["event"]["url"] = "https://eventbrite.example/e/9?utm_source=x"
    storage.save_event(seed_scrape(db, batch_id="b2"), other, client=db, now=NOW)
    assert len(db.t["events"]) == 1


@pytest.mark.parametrize("url,key", [
    ("https://WWW.Conf.Example/x/?utm_source=a&ref=b&fbclid=1#top", "conf.example/x"),
    ("https://conf.example/e?event_id=123&utm_medium=m", "conf.example/e?event_id=123"),
])
def test_canonical_url_strips_only_tracking(url, key):
    assert storage.canonical_url(url) == key


def test_person_key_rules():
    assert storage.person_key({"name": "A", "linkedin_url": "https://www.linkedin.com/in/Jane-Doe/?x=1"}) == "li:jane-doe"
    assert storage.person_key({"name": "Jane Doe!"}, "acme.com") == "nm:janedoe@acme.com"
    assert storage.person_key({"name": "Jane Doe"}, None, 7) == "nm:janedoe@event:7"
    assert storage.person_key({"name": "!!!"}) is None

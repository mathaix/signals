"""Store scraped data in Supabase (table `scraped_pages`, see schema.sql)."""
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse


def _client():
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def save_to_supabase(record: dict) -> None:
    """Insert a snapshot row; if the record has an `id` (a queued batch row) fill that row in."""
    table = _client().table("scraped_pages")
    if "id" in record:
        row = {k: v for k, v in record.items() if k != "id"}
        table.update(row).eq("id", record["id"]).execute()
    else:
        table.insert(record).execute()


def queue_batch(batch_id: str, urls: list) -> list:
    """Create one 'queued' row per URL; returns [{"id", "url"}, ...]."""
    from scraper import domain_of

    rows = [{"url": u, "domain": domain_of(u), "status": "queued", "batch_id": batch_id} for u in urls]
    return _client().table("scraped_pages").insert(rows).execute().data


def get_batch(batch_id: str) -> list:
    res = (
        _client()
        .table("scraped_pages")
        .select("url,domain,title,status,error,signals")
        .eq("batch_id", batch_id)
        .order("id")
        .execute()
    )
    return res.data


# ---- event pages (tables from schema.sql: events, people, companies, scrapes, appearances) ----

FAN_OUT_CAP = 200  # child scrapes spawned per event page; the rest stay queued
RESCRAPE_AFTER = timedelta(days=30)
APPEARANCE_KEY = "event_id,company_id,person_id,role"
LINKEDIN_SLUG = re.compile(r"https?://[\w.]*linkedin\.com/in/([^/?#]+)", re.I)
TRACKING_PARAMS = {"ref", "fbclid", "gclid", "mc_cid", "mc_eid"}


def _name_key(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def canonical_url(url: str) -> str:
    """Event identity: lowercase host without www., no fragment, no trailing slash, tracking params dropped."""
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS])
    return (p.hostname or "").removeprefix("www.") + p.path.rstrip("/") + ("?" + query if query else "")


def person_key(person: dict, company_domain=None, event_id=None) -> str | None:
    li = LINKEDIN_SLUG.match(person.get("linkedin_url") or "")
    if li:
        return "li:" + li.group(1).lower()
    key = _name_key(person.get("name"))
    if not key:
        return None
    return f"nm:{key}@{company_domain}" if company_domain else f"nm:{key}@event:{event_id}"


def _chunks(items, n=100):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _select_in(table, columns, column, values, **eq):
    rows = []
    for chunk in _chunks(sorted(values)):
        q = table.select(columns).in_(column, chunk)
        for k, v in eq.items():
            q = q.eq(k, v)
        rows += q.execute().data
    return rows


def save_event(scrape_id: int, result: dict, client=None, now=None, cap=FAN_OUT_CAP) -> dict:
    """Persist eventpage.extract() output for scrape `scrape_id` and queue child scrapes for new companies.

    Every write is an upsert on a natural key, so re-running it for the same page adds no rows; appearances carry
    last_scrape_id, which is what `still_listed` compares. Only a depth-0 scrape (no parent) fans out.
    Returns {"event_id", "children": [{"id", "url"}, ...] to spawn (at most `cap`), "overflow": rows left queued}.
    """
    c = client or _client()
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    scrape = c.table("scrapes").select("id,parent_id,batch_id").eq("id", scrape_id).execute().data[0]
    ev = result["event"]

    event_key = canonical_url(ev["url"])
    title_key = _name_key(ev.get("title"))
    if ev.get("starts_on") and title_key:  # the same event listed on another host
        same = c.table("events").select("event_key").eq("event_date", ev["starts_on"]).eq("title_key", title_key).order("id").limit(1).execute().data
        event_key = same[0]["event_key"] if same else event_key
    row = {"event_key": event_key, "kind": "event", "title": ev.get("title"), "url": ev["url"],
           "event_date": ev.get("starts_on"), "ends_on": ev.get("ends_on"), "location": ev.get("location"),
           "last_seen_at": stamp, "last_scrape_id": scrape_id}
    event_id = c.table("events").upsert({k: v for k, v in row.items() if v is not None}, on_conflict="event_key").execute().data[0]["id"]

    # companies: discover only (link text never overwrites anything), then read back ids
    names = {}
    for co in result["companies"]:
        names.setdefault(co["domain"], co.get("name"))
    if names:
        c.table("companies").upsert([{"domain": d, "name": n} for d, n in names.items()], on_conflict="domain", ignore_duplicates=True).execute()
    found = {r["domain"]: r for r in _select_in(c.table("companies"), "id,domain,last_scraped_at,merged_into", "domain", names)}

    rows = {}
    for co in result["companies"]:
        company = found[co["domain"]]
        cid = company["merged_into"] or company["id"]
        rows.setdefault((cid, co["role"]), {"event_id": event_id, "company_id": cid, "person_id": None, "role": co["role"],
                                            "evidence": co.get("evidence"), "last_seen_at": stamp, "last_scrape_id": scrape_id})
    if rows:
        c.table("appearances").upsert(list(rows.values()), on_conflict=APPEARANCE_KEY).execute()

    by_name = {_name_key(co.get("name")): found[co["domain"]] for co in result["companies"] if co.get("name")}
    by_name.update({_name_key(d.split(".")[0]): found[d] for d in names})
    rows = {}
    for p in result["people"]:
        employer = by_name.get(_name_key(p.get("company_name")))
        domain = employer["domain"] if employer else None
        key = person_key(p, domain, event_id)
        if key is None:
            continue
        person = {"person_key": key, "name": p["name"], "job_title": p.get("job_title"), "linkedin_url": p.get("linkedin_url"),
                  "company_id": employer and (employer["merged_into"] or employer["id"]),
                  "company_name": p.get("company_name"), "last_seen_at": stamp, "last_scrape_id": scrape_id}
        saved = c.table("people").upsert({k: v for k, v in person.items() if v is not None}, on_conflict="person_key").execute().data[0]
        rows.setdefault(saved["id"], {"event_id": event_id, "company_id": None, "person_id": saved["id"], "role": "speaker",
                                      "evidence": p.get("evidence"), "job_title": p.get("job_title"),
                                      "company_name": p.get("company_name"), "person_company_id": person["company_id"],
                                      "last_seen_at": stamp, "last_scrape_id": scrape_id})
    if rows:
        c.table("appearances").upsert(list(rows.values()), on_conflict=APPEARANCE_KEY).execute()

    queued = _fan_out(c, scrape, [r for r in found.values() if not r["merged_into"]], names, now)
    return {"event_id": event_id, "children": queued[:cap], "overflow": queued[cap:]}


def _fan_out(c, scrape, companies, names, now):
    """Queue one child scrape per discovered company not scraped in the last 30 days (and not already queued)."""
    if scrape["parent_id"] is not None:  # only depth-0 scrapes fan out
        return []
    cutoff = now - RESCRAPE_AFTER
    due = {r["domain"] for r in companies
           if r["last_scraped_at"] is None or datetime.fromisoformat(r["last_scraped_at"]) < cutoff}
    urls = {d: "https://" + d for d in names if d in due}  # page order
    if not urls:
        return []
    queued = {r["url"] for r in _select_in(c.table("scrapes"), "url", "url", urls.values(), status="queued")}
    rows = [{"url": u, "status": "queued", "batch_id": scrape["batch_id"], "parent_id": scrape["id"]}
            for u in urls.values() if u not in queued]
    return c.table("scrapes").insert(rows).execute().data if rows else []

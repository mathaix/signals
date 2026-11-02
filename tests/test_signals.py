import http.server
import json
import threading
from datetime import date, timedelta

import httpx
import pytest

from fetch import HTML, Fetcher, FetchError
from htmlparse import parse
from scraper import scrape
from signals import company_events

TODAY = date.today()
RECENT = [(TODAY - timedelta(days=d)).isoformat() for d in (3, 40)]
OLD = (TODAY - timedelta(days=200)).isoformat()

HOME = """<html><head><title>Acme Inc</title>
<meta name="description" content="Acme makes anvils">
<meta property="og:site_name" content="Acme"><meta property="og:image" content="/og.png">
<link rel="canonical" href="https://acme.example/">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme Corp",
"logo":"/logo.png","sameAs":["https://www.linkedin.com/company/acme","https://twitter.com/acme"]}</script>
<script src="https://js.hs-scripts.com/123.js"></script>
<script src="https://www.google-analytics.com/analytics.js"></script>
</head><body><h1>Anvils for everyone</h1><p>We sell anvils.</p>
<a href="https://boards.greenhouse.io/acme">Careers</a>
<a href="https://github.com/acme">GitHub</a></body></html>"""

SITEMAP = f"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://acme.example/blog/one</loc><lastmod>{RECENT[0]}</lastmod></url>
<url><loc>https://acme.example/blog/two</loc><lastmod>{RECENT[1]}T10:00:00Z</lastmod></url>
<url><loc>https://acme.example/blog/old</loc><lastmod>{OLD}</lastmod></url>
<url><loc>https://acme.example/about</loc><lastmod>{TODAY.isoformat()}</lastmod></url></urlset>"""


@pytest.fixture
def fixture_site():
    pages = {"/": ("text/html", HOME), "/sitemap.xml": ("application/xml", SITEMAP)}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in pages:
                self.send_response(404)
                self.end_headers()
                return
            ctype, body = pages[self.path]
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def make_fetcher():
    real = httpx.HTTPTransport()

    class Router(httpx.BaseTransport):
        def handle_request(self, request):
            if request.url.host == "boards-api.greenhouse.io":
                assert request.url.path == "/v1/boards/acme/jobs"
                return httpx.Response(200, json={"jobs": [{"id": 1}, {"id": 2}, {"id": 3}]})
            return real.handle_request(request)

    return Fetcher(allow_private=True, transport=Router())


def test_fixture_site_signals(fixture_site):
    r = scrape(fixture_site + "/", fetcher=make_fetcher())
    assert r["signals"] == {
        "profile": {
            "name": "Acme Corp",
            "description": "Acme makes anvils",
            "logo": fixture_site + "/logo.png",
            "canonical_url": "https://acme.example/",
            "socials": {"linkedin": "https://www.linkedin.com/company/acme", "x": "https://twitter.com/acme",
                        "github": "https://github.com/acme", "facebook": None, "instagram": None, "youtube": None},
            "jsonld_types": ["Organization"],
        },
        "tech": ["google-analytics", "hubspot"],
        "hiring": {"careers_url": "https://boards.greenhouse.io/acme", "ats": "greenhouse", "open_roles": 3},
        "activity": {"latest_post_date": RECENT[0], "posts_last_90d": 2, "source": "sitemap"},
    }


def test_rss_fallback_and_lever_page():
    from signals import _feed_dates

    rss = "<rss><channel><item><pubDate>Tue, 10 Jun 2025 08:00:00 GMT</pubDate></item></channel></rss>"
    assert _feed_dates(rss) == [date(2025, 6, 10)]
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><updated>2025-01-02T00:00:00Z</updated></entry></feed>'
    assert _feed_dates(atom) == [date(2025, 1, 2)]


# ---- company_events ----

EVENTS_HOME = """<html><head><title>Acme</title>
<script type="application/ld+json">{"@type":"Event","name":"Acme Anvil Meetup","startDate":"2025-06-12T18:00:00Z",
"url":"https://acme.example/meetups/anvil?utm_source=x","location":{"name":"Berlin"},
"eventAttendanceMode":"https://schema.org/OfflineEventAttendanceMode"}</script></head><body>
<a href="/events">Events</a><a href="/press">Press</a><a href="https://investors.acme.example/">Investors</a>
<a href="https://www.eventbrite.com/e/acme-webinar-123">Acme webinar: scaling</a>
<a href="https://lu.ma/">Luma home</a></body></html>"""
EVENTS_PAGE = """<html><body><script type="application/ld+json">{"@graph":[
{"@type":"BusinessEvent","name":"Anvil Summit","startDate":"2025-09-01","eventAttendanceMode":"OnlineEventAttendanceMode"},
{"@type":"Event","name":"Acme Anvil Meetup","startDate":"2025-06-12","url":"https://acme.example/meetups/anvil"}]}
</script><a href="https://www.meetup.com/acme-berlin/events/9/">Berlin meetup</a></body></html>"""
PRESS_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Acme raises $12.5 million Series A led by Foo</title><link>https://acme.example/press/series-a</link>
<pubDate>Tue, 10 Jun 2025 08:00:00 GMT</pubDate></item>
<item><title>Acme closes $1.2B Series A extension</title><link>https://acme.example/press/series-a-ext?utm_medium=x</link></item>
<item><title>Acme seed funding announced</title><link>https://acme.example/press/seed</link>
<pubDate>Mon, 01 Jan 2024 08:00:00 GMT</pubDate></item>
<item><title>Acme named a leader in anvils</title><link>https://acme.example/press/leader/</link>
<pubDate>Wed, 11 Jun 2025 08:00:00 GMT</pubDate></item>
<item><title>A series of unfortunate anvils</title><link>https://acme.example/press/series-of</link></item>
</channel></rss>"""
IR_PAGE = """<html><body><ul>
<li><a href="/q2">Q2 2025 Results</a> <span>August 5, 2025</span></li>
<li><span>2025-05-06</span><a href="/q1">First quarter earnings call</a></li>
<li><a href="/q4">Fourth quarter results</a></li>
<li><a href="/misc">Earnings</a></li>
<li><a href="/gov">Corporate governance overview</a></li></ul></body></html>"""


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url, accept=HTML, robots=True):
        self.calls.append(url)
        if url not in self.pages:
            raise FetchError("404")
        ctype, body = self.pages[url]
        return url, body, ctype


def _events(pages, home=EVENTS_HOME, base="https://acme.example/"):
    f = FakeFetcher(pages)
    return company_events(f, parse(home, base), base), f


ALL_PAGES = {
    "https://acme.example/events": ("text/html", EVENTS_PAGE),
    "https://acme.example/press": ("application/rss+xml", PRESS_RSS),
    "https://investors.acme.example/": ("text/html", IR_PAGE),
}


def by_key(evs):
    return {e["event_key"]: e for e in evs}


def test_company_events_fixtures():
    evs, f = _events(ALL_PAGES)
    assert f.calls == list(ALL_PAGES)
    assert [e["event_key"] for e in evs] == [
        "https://acme.example/meetups/anvil",
        "https://eventbrite.com/e/acme-webinar-123",
        "https://acme.example/events#anvilsummit",
        "https://meetup.com/acme-berlin/events/9",
        "funding:acme.example:series a",  # the "Series A extension" headline merges into the same round
        "funding:acme.example:seed",
        "https://acme.example/press/leader",
        "https://acme.example/press/series-of",
        "earnings:acme.example:2025-08-05",
        "earnings:acme.example:2025-05-06",
        "https://investors.acme.example/q4",
    ]
    k = by_key(evs)
    meetup = k["https://acme.example/meetups/anvil"]
    assert (meetup["kind"], meetup["role"], meetup["event_date"], meetup["location"], meetup["evidence"]) == (
        "meetup", "host", "2025-06-12", "Berlin", "jsonld:Event")
    assert meetup["source_url"] == "https://acme.example/"
    assert k["https://eventbrite.com/e/acme-webinar-123"]["kind"] == "webinar"
    summit = k["https://acme.example/events#anvilsummit"]
    assert (summit["kind"], summit["event_date"], summit["source_url"]) == (
        "webinar", "2025-09-01", "https://acme.example/events")
    assert k["https://meetup.com/acme-berlin/events/9"]["evidence"] == "link:www.meetup.com"
    a = k["funding:acme.example:series a"]
    assert (a["kind"], a["round"], a["amount_usd"], a["role"], a["event_date"]) == (
        "funding", "series a", 12_500_000, "subject", "2025-06-10")
    assert a["url"] == "https://acme.example/press/series-a" and a["source_url"] == "https://acme.example/press"


def test_company_events_funding_rules():
    evs, _ = _events(ALL_PAGES)
    k = by_key(evs)
    seed = k["funding:acme.example:seed"]
    assert (seed["round"], seed["amount_usd"], seed["event_date"]) == ("seed", None, "2024-01-01")
    leader = k["https://acme.example/press/leader"]
    assert (leader["kind"], leader["role"], leader["round"], leader["event_date"]) == ("press", "subject", None, "2025-06-11")
    # lowercase "series of" is not a round
    assert k["https://acme.example/press/series-of"]["kind"] == "press"
    # undated stays undated (Q3 is applied by the query)
    assert k["https://acme.example/press/series-of"]["event_date"] is None


def test_company_events_earnings():
    evs, _ = _events(ALL_PAGES)
    e = [x for x in evs if x["kind"] == "earnings"]
    assert [(x["event_key"], x["event_date"], x["role"]) for x in e] == [
        ("earnings:acme.example:2025-08-05", "2025-08-05", "subject"),
        ("earnings:acme.example:2025-05-06", "2025-05-06", "subject"),
        ("https://investors.acme.example/q4", None, "subject"),
    ]


def test_company_events_amounts_and_rounds():
    from signals import _funding

    assert _funding("Acme raises $1.2B Series C") == (True, "series c", 1_200_000_000)
    assert _funding("Acme lands $30m seed") == (True, "seed", 30_000_000)
    assert _funding("Acme raises $2,500 million") == (True, None, 2_500_000_000)
    assert _funding("Acme raises money") == (True, None, None)
    assert _funding("Acme ships anvils") == (False, None, None)


def test_company_events_same_round_merges_and_rerun_is_stable():
    rss = """<rss><channel>
<item><title>Acme raises Series B</title><link>https://acme.example/press/b1</link></item>
<item><title>Acme raises $40 million Series B</title><link>https://acme.example/blog/b2</link>
<pubDate>Tue, 10 Jun 2025 08:00:00 GMT</pubDate></item></channel></rss>"""
    pages = {"https://acme.example/press": ("text/xml", rss)}
    evs, _ = _events(pages)
    b = [e for e in evs if e["kind"] == "funding"]
    assert len(b) == 1
    assert (b[0]["amount_usd"], b[0]["event_date"], b[0]["url"]) == (40_000_000, "2025-06-10", "https://acme.example/press/b1")
    assert _events(pages)[0] == evs


def test_company_events_atom_and_request_cap():
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Acme raises seed</title>'
            '<link href="/news/seed"/><updated>2025-02-03T00:00:00Z</updated></entry></feed>')
    home = '<a href="/news">News</a>'
    evs, f = _events({"https://acme.example/news": ("application/atom+xml", atom)}, home=home)
    assert [(e["event_key"], e["event_date"], e["url"]) for e in evs] == [
        ("funding:acme.example:seed", "2025-02-03", "https://acme.example/news/seed")]
    evs, f = _events(ALL_PAGES)
    assert len(f.calls) <= 3


def test_company_events_failures_and_empty():
    evs, f = _events({})  # every follow-up 404s
    assert [e["kind"] for e in evs] == ["meetup", "webinar"]  # homepage-only items survive
    assert len(f.calls) == 3
    assert _events({}, home="<html><a href='/x'>x</a></html>")[0] == []
    assert _events({"https://acme.example/press": ("application/rss+xml", "<rss")}, home="<a href='/press'>p</a>")[0] == []


def test_company_events_real_fetcher():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        body = {"/press": ("application/rss+xml", PRESS_RSS)}.get(request.url.path)
        if not body:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": body[0]}, content=body[1])

    f = Fetcher(allow_private=True, transport=httpx.MockTransport(handler))
    evs = company_events(f, parse('<a href="/press">Press</a>', "https://acme.example/"), "https://acme.example/")
    assert by_key(evs)["funding:acme.example:series a"]["amount_usd"] == 12_500_000

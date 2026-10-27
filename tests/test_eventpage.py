from pathlib import Path

from eventpage import extract, role_from_heading
from htmlparse import parse, registrable_domain
from scraper import registrable_domain as scraper_registrable_domain

FIXTURES = Path(__file__).parent / "fixtures"


def load(name, base):
    return parse((FIXTURES / name).read_text(), base)


def pairs(result):
    return [(c["domain"], c["role"], c["evidence"]) for c in result["companies"]]


def test_sponsor_exhibitor_page_companies_roles_and_event():
    r = extract(load("event_sponsors.html", "https://www.saasexpo.example/2026"))
    assert r["event"] == {
        "title": "SaaS Expo 2026", "starts_on": "2026-11-10", "ends_on": "2026-11-11",
        "location": "Moscone Center, San Francisco, CA", "url": "https://saasexpo.example/2026",
    }
    # page order; role + evidence from the nearest preceding heading; same-domain/social/ticketing/CDN skipped
    assert pairs(r) == [
        ("acme.com", "sponsor", "Gold Sponsors"),
        ("initech.com", "sponsor", "Gold Sponsors"),
        ("globex.com", "exhibitor", "Exhibitors"),
        ("acme.com", "exhibitor", "Exhibitors"),  # jobs.acme.com -> acme.com
        ("umbrella.co.uk", "exhibitor", "Exhibitors"),
        ("hooli.io", "partner", "Community partners"),
        ("stark.com", "listed", "About"),  # no role keyword in the heading
    ]
    by = {(c["domain"], c["role"]): c for c in r["companies"]}
    assert by["initech.com", "sponsor"]["name"] == "Initech"
    assert by["acme.com", "sponsor"]["name"] is None  # logo-only link has no text
    assert r["people"] == []


def test_listed_mention_dropped_when_company_has_a_real_role():
    r = extract(load("event_sponsors.html", "https://saasexpo.example/2026"))
    assert ("initech.com", "listed") not in {(c["domain"], c["role"]) for c in r["companies"]}  # also linked under About


def test_link_before_any_heading_is_listed_without_evidence():
    r = extract(parse('<a href="https://acme.com">Acme</a><h2>Speakers</h2><a href="https://globex.com">G</a>', "https://x.example/"))
    assert pairs(r) == [("acme.com", "listed", None), ("globex.com", "speaker", "Speakers")]


def test_luma_page_with_jsonld_people_and_virtual_location():
    r = extract(load("event_luma.html", "https://lu.ma/founders"))
    assert r["event"]["title"] == "Founders Night"
    assert (r["event"]["starts_on"], r["event"]["ends_on"], r["event"]["location"]) == ("2026-12-03", "2026-12-03", "Zoom")
    assert pairs(r) == [("hooli.io", "listed", "Hosted by")]  # lu.ma and linkedin skipped
    assert r["people"] == [
        {"name": "Alice Smith", "job_title": "CTO", "company_name": "Initech", "role": "speaker",
         "linkedin_url": "https://www.linkedin.com/in/alice-smith", "evidence": "jsonld:performer"},
        {"name": "Bob Jones", "job_title": "Founder", "company_name": None, "role": "speaker",
         "linkedin_url": None, "evidence": "jsonld:performer"},
    ]  # Alice's LinkedIn link in the page body does not create a second person


def test_speaker_grid_cards_with_linkedin():
    r = extract(load("event_speakers.html", "https://devday.example/"))
    got = [(p["name"], p["job_title"], p["company_name"], p["linkedin_url"]) for p in r["people"]]
    assert got == [
        ("Jane Doe", "VP Marketing", "Globex", "https://www.linkedin.com/in/janedoe"),
        ("John Roe", "CTO", "Initech Inc", "https://www.linkedin.com/in/johnroe"),
        ("May Smith", "Head of Growth", "Acme", "https://www.linkedin.com/in/maysmith"),
    ]  # "No Link Person" has no LinkedIn link, so it is not a card
    assert pairs(r) == [("acme.com", "partner", "Partners")]


def test_grid_without_card_wrappers_does_not_mix_people():
    html = '<h2>Speakers</h2><div><h3>A One</h3><p>CEO, Foo</p><a href="https://linkedin.com/in/a">in</a>' \
           '<h3>B Two</h3><p>CTO, Bar</p><a href="https://linkedin.com/in/b">in</a></div>'
    assert extract(parse(html, "https://x.example/"))["people"] == []  # ambiguous container: skip rather than guess


def test_namesakes_with_different_linkedin_stay_separate_and_missing_data_is_none():
    html = ('<div><h3>Sam Lee</h3><a href="https://linkedin.com/in/sam1">in</a></div>'
            '<div><h3>Sam Lee</h3><a href="https://linkedin.com/in/sam2">in</a></div>')
    people = extract(parse(html, "https://x.example/"))["people"]
    assert [(p["name"], p["job_title"], p["company_name"]) for p in people] == [("Sam Lee", None, None)] * 2
    assert len({p["linkedin_url"] for p in people}) == 2


def test_event_without_jsonld_falls_back_to_og_title_and_leaves_dates_unknown():
    r = extract(parse('<meta property="og:title" content="Meetup"><h1>x</h1>', "https://x.example/e"))
    assert r["event"] == {"title": "Meetup", "starts_on": None, "ends_on": None, "location": None, "url": "https://x.example/e"}


def test_role_from_heading():
    assert role_from_heading("Platinum Sponsors") == "sponsor"
    assert role_from_heading("Our Organisers") == "organizer"
    assert role_from_heading("Sponsors & Partners") == "sponsor"  # earliest keyword
    assert role_from_heading("Hosted by") == "listed"
    assert role_from_heading(None) == "listed"


def test_htmlparse_records_last_h1_to_h4_before_each_anchor():
    p = parse('<a href="/0">z</a><h1>One</h1><a href="/1">a</a><h4>Four <b>x</b></h4><a href="/2">b</a><a href="/3">c</a>'
              '<h5>not a heading</h5><a href="/4">d</a>', "https://x.example/")
    assert p.anchor_headings == [None, "One", "Four x", "Four x", "Four x"]
    assert len(p.anchor_headings) == len(p.anchors) == len(p.anchor_paths)


def test_registrable_domain():
    assert {registrable_domain(h) for h in ("jobs.acme.com", "www.acme.com", "ACME.com")} == {"acme.com"}
    assert registrable_domain("www.acme.co.uk") == "acme.co.uk"
    assert scraper_registrable_domain("a.b.acme.com.au") == "acme.com.au"

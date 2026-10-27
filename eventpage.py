"""Rule-based extraction from an event page: the event, the companies listed on it and its people."""
import re
from urllib.parse import urlparse

from htmlparse import registrable_domain
from signals import SOCIAL_HOSTS

ROLE_RE = re.compile(r"sponsor|exhibitor|partner|organi[sz]er|speaker", re.I)
ROLES = {"sponsor", "exhibitor", "partner", "organizer", "speaker"}

# Hosts that are never a company the event lists: social, ticketing/event platforms, CDNs and link shorteners.
SKIP_DOMAINS = set(SOCIAL_HOSTS) | {
    "t.co", "linktr.ee", "tiktok.com", "wa.me", "t.me", "telegram.org", "discord.com", "discord.gg", "slack.com",
    "eventbrite.com", "lu.ma", "luma.com", "meetup.com", "hopin.com", "sessionize.com", "ticketmaster.com",
    "universe.com", "tito.io", "ti.to", "cvent.com", "humanitix.com", "splashthat.com", "eventzilla.net",
    "bit.ly", "goo.gl", "tinyurl.com", "ow.ly",
    "cloudfront.net", "amazonaws.com", "googleapis.com", "gstatic.com", "googleusercontent.com", "google.com",
    "cloudflare.com", "jsdelivr.net", "unpkg.com", "wp.com", "gravatar.com", "fastly.net", "akamaihd.net",
    "imgix.net", "cloudinary.com", "w.org", "wordpress.org", "apple.com",
}
GENERIC_TEXT = {"", "website", "visit website", "learn more", "read more", "visit", "link", "logo", "more"}
LINKEDIN_IN = re.compile(r"^https?://([\w-]+\.)?linkedin\.com/in/([^/?#\s]+)", re.I)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
LABELS = {"linkedin", "in", "profile", "view profile", "linkedin profile"}


def skipped(host: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS)


def linkedin_url(url) -> str | None:
    m = LINKEDIN_IN.match(url) if isinstance(url, str) else None
    return f"https://www.linkedin.com/in/{m.group(2).lower()}" if m else None


def role_from_heading(heading) -> str:
    m = ROLE_RE.search(heading or "")
    if not m:
        return "listed"
    role = m.group(0).lower()
    return "organizer" if role.startswith("organi") else role


def extract(page) -> dict:
    """page is an htmlparse.Page. Returns {"event": {...}, "companies": [...], "people": [...]}."""
    event_node = _event_node(page.jsonld)
    return {
        "event": _event_fields(page, event_node),
        "companies": _companies(page),
        "people": _merge_people(_jsonld_people(page.jsonld, event_node) + _card_people(page)),
    }


def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v)


def _types(node):
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def _is(node, name):
    return any(t == name or t.endswith(name) for t in _types(node))


def _event_node(jsonld):
    return next((n for n in _walk(jsonld) if _is(n, "Event")), None)


def _text(v):
    if isinstance(v, dict):
        v = v.get("name")
    return " ".join(v.split()) if isinstance(v, str) and v.strip() else None


def _date(v):
    m = DATE_RE.match(v.strip()) if isinstance(v, str) else None
    return m.group(0) if m else None


def _location(loc):
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        return _text(loc)
    if not isinstance(loc, dict):
        return None
    addr = loc.get("address")
    if isinstance(addr, dict):
        addr = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "addressCountry")]
        addr = [_text(x) if not isinstance(x, dict) else _text(x.get("name")) for x in addr]
    elif isinstance(addr, str):
        addr = [addr]
    else:
        addr = []
    parts = [p for p in [_text(loc.get("name"))] + addr if p]
    return ", ".join(dict.fromkeys(parts)) or ("Online" if _is(loc, "VirtualLocation") else None)


def _event_fields(page, node):
    node = node or {}
    title = _text(node.get("name")) or page.meta.get("og:title") or page.title or (page.headings or [None])[0]
    return {
        "title": title or None,
        "starts_on": _date(node.get("startDate")),
        "ends_on": _date(node.get("endDate")),
        "location": _location(node.get("location")),
        "url": page.canonical or page.base,
    }


def _companies(page) -> list:
    own = registrable_domain(urlparse(page.base).hostname or "")
    found = {}  # (domain, role) -> company, first mention wins
    for (url, text), heading in zip(page.anchors, page.anchor_headings):
        host = (urlparse(url).hostname or "").lower()
        domain = registrable_domain(host)
        if not host or domain == own or skipped(host):
            continue
        name = " ".join(text.split())
        role = role_from_heading(heading)
        company = found.setdefault((domain, role), {
            "domain": domain, "url": url, "name": None, "role": role, "evidence": heading,
        })
        if company["name"] is None and name.lower() not in GENERIC_TEXT:
            company["name"] = name
    # a bare mention elsewhere on the page adds nothing once the company has a real role
    roled = {d for d, r in found if r != "listed"}
    return [c for (d, r), c in found.items() if r != "listed" or d not in roled]


def _person(name, job_title, company_name, linkedin, evidence):
    return {"name": name, "job_title": job_title, "company_name": company_name,
            "linkedin_url": linkedin, "role": "speaker", "evidence": evidence}


def _same_as(node):
    same = node.get("sameAs", [])
    for u in [same] if isinstance(same, str) else same if isinstance(same, list) else []:
        if linkedin_url(u):
            return linkedin_url(u)
    return linkedin_url(node.get("url"))


def _jsonld_people(jsonld, event_node):
    nodes = []
    if event_node:
        for key in ("performer", "performers"):
            nodes += [n for n in _walk(event_node.get(key)) if _is(n, "Person")]
    for n in jsonld if isinstance(jsonld, list) else [jsonld]:  # top-level (or @graph) Person blocks
        for m in (n.get("@graph", [n]) if isinstance(n, dict) else []):
            if isinstance(m, dict) and _is(m, "Person"):
                nodes.append(m)
    people = []
    for n in nodes:
        name = _text(n.get("name"))
        if name:
            people.append(_person(name, _text(n.get("jobTitle")), _text(n.get("worksFor")), _same_as(n), "jsonld:performer"))
    return people


def _split_title(line):
    """'CTO at Initech' / 'CTO @ Initech' / 'CTO, Initech' -> (title, employer) as printed."""
    for sep in (" at ", " @ "):
        if sep in line:
            t, c = line.split(sep, 1)
            return t.strip() or None, c.strip() or None
    if ", " in line:
        t, c = line.rsplit(", ", 1)
        return t.strip(), c.strip()
    return line, None


def _card_people(page) -> list:
    """A speaker card is the smallest container holding exactly one LinkedIn profile link."""
    per_container = {}
    for (url, _), path in zip(page.anchors, page.anchor_paths):
        if linkedin_url(url):
            for cid in path:
                per_container.setdefault(cid, set()).add(linkedin_url(url))
    people = []
    for (url, text), path in zip(page.anchors, page.anchor_paths):
        li = linkedin_url(url)
        if not li:
            continue
        card = next((c for c in reversed(path) if per_container[c] == {li}), None)
        if card is None:
            continue
        lines = [t for p, t in page.lines if card in p and t.lower() not in LABELS]
        if not lines:
            continue
        title, employer = (None, None)
        if len(lines) > 1:
            title, employer = _split_title(lines[1])
            if len(lines) > 2 and employer is None:
                employer = lines[2]
        people.append(_person(lines[0], title, employer, li, "speaker card"))
    return people


def _fill(have, p):
    for k, v in p.items():
        if have.get(k) is None:
            have[k] = v


def _merge_people(people) -> list:
    """One person per LinkedIn URL; one without a URL joins the same-named person, else stands alone."""
    out, by_li, by_name = [], {}, {}
    for p in people:
        have = by_li.get(p["linkedin_url"]) if p["linkedin_url"] else None
        if have is None and not p["linkedin_url"]:
            have = by_name.get(p["name"].lower())
        if have is None and p["linkedin_url"]:
            have = next((q for q in out if not q["linkedin_url"] and q["name"].lower() == p["name"].lower()), None)
        if have is None:
            have = p
            out.append(p)
        else:
            _fill(have, p)
        if have["linkedin_url"]:
            by_li[have["linkedin_url"]] = have
        by_name.setdefault(have["name"].lower(), have)
    return out

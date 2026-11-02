"""Marketing signals from a parsed homepage plus a few polite follow-up fetches."""
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

from fetch import HTML, JSON, XML, FetchError

log = logging.getLogger(__name__)

SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "x.com": "x",
    "twitter.com": "x",
    "github.com": "github",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "youtube.com": "youtube",
}
ORG_TYPES = {"Organization", "Corporation", "LocalBusiness", "OnlineBusiness", "NGO"}

# substring of a <script src>/<link href> URL -> tool
ASSET_RULES = {
    "google-analytics.com": "google-analytics",
    "googletagmanager.com/gtag": "google-analytics",
    "googletagmanager.com/gtm": "google-tag-manager",
    "segment.com": "segment",
    "segment.io": "segment",
    "hs-scripts.com": "hubspot",
    "hs-analytics.net": "hubspot",
    "hsforms.net": "hubspot",
    "hubspot.com": "hubspot",
    "munchkin.marketo.net": "marketo",
    "marketo.net": "marketo",
    "pi.pardot.com": "pardot",
    "pardot.com": "pardot",
    "salesforce.com": "salesforce",
    "force.com": "salesforce",
    "intercom.io": "intercom",
    "intercomcdn.com": "intercom",
    "drift.com": "drift",
    "driftt.com": "drift",
    "zdassets.com": "zendesk",
    "zendesk.com": "zendesk",
    "hotjar.com": "hotjar",
    "mixpanel.com": "mixpanel",
    "amplitude.com": "amplitude",
    "posthog.com": "posthog",
    "js.stripe.com": "stripe",
    "cdn.shopify.com": "shopify",
    "myshopify.com": "shopify",
    "/wp-content/": "wordpress",
    "/wp-includes/": "wordpress",
    "webflow.com": "webflow",
    "website-files.com": "webflow",
    "parastorage.com": "wix",
    "wixstatic.com": "wix",
    "squarespace.com": "squarespace",
    "sqspcdn.com": "squarespace",
    "cdnjs.cloudflare.com": "cloudflare",
    "cloudflareinsights.com": "cloudflare",
    "vercel.live": "vercel",
    "/_vercel/": "vercel",
    "/_next/": "nextjs",
    "connect.facebook.net": "facebook-pixel",
    "snap.licdn.com": "linkedin-insight",
    "clarity.ms": "microsoft-clarity",
    "cookiebot.com": "cookiebot",
    "calendly.com": "calendly",
    "typeform.com": "typeform",
    "fullstory.com": "fullstory",
    "heap.io": "heap",
    "crisp.chat": "crisp",
}
# substring of <meta name=generator> -> tool
GENERATOR_RULES = {
    "wordpress": "wordpress",
    "webflow": "webflow",
    "wix": "wix",
    "squarespace": "squarespace",
    "shopify": "shopify",
    "hubspot": "hubspot",
    "ghost": "ghost",
    "next.js": "nextjs",
    "gatsby": "gatsby",
    "framer": "framer",
}

ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
}
ATS_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
}
CAREERS_RE = re.compile(r"\b(careers?|jobs|join us|join our team|we'?re hiring|work with us)\b", re.I)
CAREERS_PATH_RE = re.compile(r"/(careers?|jobs|join-us|work-with-us)(/|$)", re.I)
BLOG_RE = re.compile(r"/(blog|news|insights)/[^/?#]+")


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _social_key(url):
    host = _host(url)
    path = urlparse(url).path.lower()
    if path.startswith(("/share", "/intent", "/sharer")) or path in ("", "/"):
        return None
    for domain, key in SOCIAL_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return key


def _ld_nodes(blocks):
    for b in blocks:
        if isinstance(b, list):
            yield from _ld_nodes(b)
        elif isinstance(b, dict):
            yield b
            yield from _ld_nodes(b.get("@graph", []))


def _types(node):
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def profile(page, final_url):
    nodes = list(_ld_nodes(page.jsonld))
    types = []
    for n in nodes:
        for t in _types(n):
            if t not in types:
                types.append(t)
    org = next((n for n in nodes if ORG_TYPES & set(_types(n))), {})
    logo = org.get("logo")
    if isinstance(logo, dict):
        logo = logo.get("url")
    logo = logo if isinstance(logo, str) and logo else page.meta.get("og:image")
    same_as = org.get("sameAs", [])
    candidates = [same_as] if isinstance(same_as, str) else [u for u in same_as if isinstance(u, str)]
    candidates += [u for u, _ in page.anchors]
    socials = dict.fromkeys(("linkedin", "x", "github", "facebook", "instagram", "youtube"))
    for u in candidates:
        key = _social_key(u)
        if key and socials[key] is None:
            socials[key] = u
    name = org.get("name") if isinstance(org.get("name"), str) else None
    return {
        "name": name or page.meta.get("og:site_name") or page.title or None,
        "description": page.meta.get("description") or page.meta.get("og:description"),
        "logo": urljoin(final_url, logo) if logo else None,
        "canonical_url": page.canonical or final_url,
        "socials": socials,
        "jsonld_types": types,
    }


def tech(page):
    found = {tool for pattern, tool in ASSET_RULES.items() for a in page.assets if pattern in a.lower()}
    gen = page.meta.get("generator", "").lower()
    found |= {tool for pattern, tool in GENERATOR_RULES.items() if pattern in gen}
    return sorted(found)


def _ats_of(url):
    host = _host(url)
    return ATS_HOSTS.get(host), (urlparse(url).path.strip("/").split("/") or [""])[0]


def _find_careers(anchors):
    for url, _ in anchors:
        if _host(url) in ATS_HOSTS:
            return url
    for url, text in anchors:
        if CAREERS_PATH_RE.search(urlparse(url).path) or CAREERS_RE.search(text):
            return url


def _count_roles(fetcher, ats, slug):
    if ats not in ATS_API or not re.fullmatch(r"[\w.-]+", slug):
        return None
    try:
        data = json.loads(fetcher.fetch(ATS_API[ats].format(slug), accept=JSON, robots=False)[1])
        jobs = data.get("jobs") if isinstance(data, dict) else data
        return len(jobs) if isinstance(jobs, list) else None
    except (FetchError, ValueError):
        return None


def hiring(fetcher, page, parse):
    out = {"careers_url": None, "ats": None, "open_roles": None}
    url = _find_careers(page.anchors)
    if not url:
        return out
    out["careers_url"] = url
    ats, slug = _ats_of(url)
    if not ats:
        try:
            final, html, _ = fetcher.fetch(url)
            sub = _find_ats(parse(html, final).anchors)
            if sub:
                ats, slug = _ats_of(sub)
        except FetchError:
            pass
    out["ats"] = ats
    if ats:
        out["open_roles"] = _count_roles(fetcher, ats, slug)
    return out


def _find_ats(anchors):
    return next((u for u, _ in anchors if _host(u) in ATS_HOSTS), None)


def _date(s):
    s = (s or "").strip()
    try:
        if re.match(r"\d{4}-\d\d-\d\d", s):
            return date.fromisoformat(s[:10])
        return parsedate_to_datetime(s).date()
    except (ValueError, TypeError):
        return None


def _local(el):
    return el.tag.rsplit("}", 1)[-1]


def _child_text(el, *names):
    for c in el:
        if _local(c) in names and c.text:
            return c.text
    return None


def _sitemap_dates(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if _local(el) == "url":
            loc, mod = _child_text(el, "loc"), _date(_child_text(el, "lastmod"))
            if loc and mod and BLOG_RE.search(urlparse(loc.strip()).path):
                out.append(mod)
    return out


def _feed_dates(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    items = [e for e in root.iter() if _local(e) in ("item", "entry")]
    dates = [_date(_child_text(i, "pubDate", "published", "updated")) for i in items]
    return [d for d in dates if d]


def activity(fetcher, page, final_url, today):
    dates, source = [], None
    origin = "{0.scheme}://{0.netloc}".format(urlparse(final_url))
    try:
        sitemap = (fetcher.sitemaps(final_url) or [origin + "/sitemap.xml"])[0]
        dates = _sitemap_dates(fetcher.fetch(sitemap, accept=XML)[1])
        source = "sitemap" if dates else None
    except FetchError:
        pass
    if not dates and page.feeds:
        try:
            dates = _feed_dates(fetcher.fetch(page.feeds[0], accept=XML)[1])
            source = "rss" if dates else None
        except FetchError:
            pass
    if not source:
        return {"latest_post_date": None, "posts_last_90d": None, "source": None}
    cutoff = today - timedelta(days=90)
    return {
        "latest_post_date": max(dates).isoformat(),
        "posts_last_90d": sum(1 for d in dates if d >= cutoff),
        "source": source,
    }


EVENT_HOSTS = ("lu.ma", "meetup.com", "hopin.com", "sessionize.com")
MAX_EVENT_REQUESTS = 3  # extra fetches company_events may make per company
MAX_ITEMS = 25  # per source
FUNDING_RE = re.compile(r"\b(raises|raised|seed|funding)\b", re.I)
SERIES_RE = re.compile(r"\b[Ss]eries ([A-Z])\b")
AMOUNT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(million|billion|m|b)\b", re.I)
EARNINGS_RE = re.compile(r"\b(results|earnings)\b", re.I)
QUARTER_RE = re.compile(r"\bQ[1-4]\b|\bquarter\b|\bfiscal\b", re.I)
EVENTS_PATH_RE = re.compile(r"^/events?/?$", re.I)
PRESS_PATH_RE = re.compile(r"^/(press|newsroom|press-releases?|news|media)/?$", re.I)
IR_PATH_RE = re.compile(r"^/(ir|investors?|investor-relations)(/|$)", re.I)
TRACKING_PARAMS = ("utm_", "ref", "fbclid", "gclid", "mc_cid", "mc_eid")
MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
TEXT_DATE_RES = (
    re.compile(r"\b(\d{4})-(\d\d)-(\d\d)\b"),
    re.compile(r"\b([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b"),
    re.compile(r"\b(\d{1,2}) ([A-Za-z]{3})[a-z]*\.?,? (\d{4})\b"),
)


def _site(url):
    return ".".join(_host(url).split(".")[-2:])


def _canon(url):
    """Canonical item URL: lowercase host, no www., no fragment, no tracking params, no trailing slash."""
    p = urlparse(url)
    q = "&".join(kv for kv in p.query.split("&")
                 if kv and not kv.split("=")[0].lower().startswith(TRACKING_PARAMS))
    host = (p.hostname or "").lower().removeprefix("www.")
    netloc = host + (f":{p.port}" if p.port else "")
    return p._replace(scheme=p.scheme.lower(), netloc=netloc, path=p.path.rstrip("/"), query=q, fragment="").geturl()


def _text_date(s):
    for i, rx in enumerate(TEXT_DATE_RES):
        m = rx.search(s or "")
        if not m:
            continue
        try:
            if i == 0:
                return date(int(m[1]), int(m[2]), int(m[3]))
            mon, day, year = (m[1], m[2], m[3]) if i == 1 else (m[2], m[1], m[3])
            return date(int(year), MONTHS[mon.lower()], int(day))
        except (KeyError, ValueError):
            continue
    return None


def _funding(title):
    """(is_funding, round, amount_usd) from a headline."""
    series = SERIES_RE.search(title)
    if not (series or FUNDING_RE.search(title)):
        return False, None, None
    rnd = f"series {series[1].lower()}" if series else ("seed" if re.search(r"\bseed\b", title, re.I) else None)
    m = AMOUNT_RE.search(title)
    amount = None
    if m:
        mult = 10**9 if m[2].lower() in ("billion", "b") else 10**6
        amount = int(Decimal(m[1].replace(",", "")) * mult)
    return True, rnd, amount


def _is_platform(url):
    host = _host(url)
    if urlparse(url).path in ("", "/"):
        return False
    return "eventbrite" in host.split(".") or any(host == h or host.endswith("." + h) for h in EVENT_HOSTS)


def _event_item(kind, key, title, url, when, source, evidence, **extra):
    role = "host" if kind in ("meetup", "webinar") else "subject"
    return {"kind": kind, "event_key": key, "title": title or None, "url": url,
            "event_date": when.isoformat() if when else None, "amount_usd": None, "round": None,
            "role": role, "evidence": evidence, "source_url": source, **extra}


def _ld_events(page, source):
    out = []
    for n in _ld_nodes(page.jsonld):
        if not any(t == "Event" or t.endswith("Event") for t in _types(n)):
            continue
        name = n.get("name") if isinstance(n.get("name"), str) else None
        mode = str(n.get("eventAttendanceMode", ""))
        online = "OnlineEventAttendanceMode" in mode and "Mixed" not in mode
        kind = "webinar" if online or "webinar" in (name or "").lower() else "meetup"
        url = n.get("url") if isinstance(n.get("url"), str) and n["url"] else None
        url = urljoin(source, url) if url else source
        key = _canon(url)
        if not n.get("url"):
            key += "#" + re.sub(r"[^a-z0-9]+", "", (name or "").lower())
        loc = n.get("location")
        loc = loc.get("name") if isinstance(loc, dict) else loc
        out.append(_event_item(kind, key, name, url, _date(str(n.get("startDate", ""))), source, "jsonld:Event",
                               location=loc if isinstance(loc, str) else None))
    return out


def _platform_links(page, source):
    out = []
    for url, text in page.anchors:
        if _is_platform(url):
            title = " ".join(text.split())
            kind = "webinar" if "webinar" in (title + url).lower() else "meetup"
            out.append(_event_item(kind, _canon(url), title, url, None, source, f"link:{_host(url)}"))
    return out


def _html_items(page, source, accept):
    """(url, title, date) for anchors passing accept(url, title); date comes from the anchor or adjacent text."""
    chunks = [" ".join(t.split()) for t in page.text]
    chunks = [c for c in chunks if c]
    out = []
    for url, text in page.anchors:
        title = " ".join(text.split())
        if not title or not accept(url, title):
            continue
        when = _text_date(title)
        if not when and title in chunks:
            i = chunks.index(title)
            when = next((d for d in (_text_date(c) for c in chunks[max(0, i - 1):i] + chunks[i + 1:i + 2]) if d), None)
        out.append((url, title, when))
    return out


def _feed_items(xml, source):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter():
        if _local(it) not in ("item", "entry"):
            continue
        link = _child_text(it, "link")
        if not link:
            link = next((c.get("href") for c in it if _local(c) == "link" and c.get("href")), None)
        title = _child_text(it, "title")
        if title and link:
            when = _date(_child_text(it, "pubDate", "published", "updated", "date"))
            out.append((urljoin(source, link.strip()), " ".join(title.split()), when))
    return out


def _press(items, source, domain, evidence):
    out = []
    for url, title, when in items[:MAX_ITEMS]:
        is_funding, rnd, amount = _funding(title)
        if is_funding:
            key = f"funding:{domain}:{rnd}" if rnd else _canon(url)
            out.append(_event_item("funding", key, title, url, when, source, f"{evidence}:funding-headline",
                                   amount_usd=amount, round=rnd))
        else:
            out.append(_event_item("press", _canon(url), title, url, when, source, f"{evidence}:press"))
    return out


def _earnings(items, source, domain):
    out = []
    for url, title, when in items:
        if not EARNINGS_RE.search(title) or not (when or QUARTER_RE.search(title)):
            continue
        key = f"earnings:{domain}:{when.isoformat()}" if when else _canon(url)
        out.append(_event_item("earnings", key, title, url, when, source, "ir:earnings"))
        if len(out) == MAX_ITEMS:
            break
    return out


def _merge(items):
    """De-duplicate by event_key in first-seen order, filling gaps in the first row from later ones."""
    merged = {}
    for it in items:
        kept = merged.setdefault(it["event_key"], it)
        if kept is not it:
            for k, v in it.items():
                if kept.get(k) is None and v is not None:
                    kept[k] = v
    return list(merged.values())


def company_events(fetcher, page, final_url):
    """Events signal: hosted/attended events, funding + press, earnings.

    Returns one dict per events row (kind, event_key, title, url, event_date, amount_usd, round, ...) plus its
    appearance link (role, evidence) and the source_url it was found at. event_date None means undated; the
    final-design Q3 rule is applied by the query against first_seen_at, not here. Makes at most
    MAX_EVENT_REQUESTS fetches: the events page, the press page/feed and the investor-relations page.
    """
    from htmlparse import parse

    domain = _host(final_url).removeprefix("www.")
    site = _site(final_url)
    budget = [MAX_EVENT_REQUESTS]

    def get(url, accept):
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            final, body, ctype = fetcher.fetch(url, accept=accept)
        except FetchError:
            return None
        return final, body, ctype

    def find(rx, same_host_only=False):
        for url, _ in page.anchors:
            p = urlparse(url)
            if _site(url) == site and rx.search(p.path) and (not same_host_only or _host(url) == _host(final_url)):
                return url

    items = _ld_events(page, final_url) + _platform_links(page, final_url)

    events_url = find(EVENTS_PATH_RE, True)
    if events_url and (got := get(events_url, HTML)):
        sub = parse(got[1], got[0])
        items += _ld_events(sub, got[0]) + _platform_links(sub, got[0])

    feed = next((f for f in page.feeds if re.search(r"press|news", urlparse(f).path, re.I)), None)
    press_url = find(PRESS_PATH_RE, True) or feed
    if press_url and (got := get(press_url, HTML + XML)):
        src, body, ctype = got
        if ctype in XML:
            found = _feed_items(body, src)
        else:
            sub = parse(body, src)
            found = _html_items(sub, src, lambda u, t: _site(u) == site and len(t.split()) >= 4
                                and not PRESS_PATH_RE.search(urlparse(u).path))
        items += _press(found, src, domain, "rss" if ctype in XML else "press-page")

    ir_url = next((u for u, _ in page.anchors if _site(u) == site and
                   (_host(u).startswith(("investors.", "investor.")) or IR_PATH_RE.search(urlparse(u).path))), None)
    if ir_url and (got := get(ir_url, HTML)):
        sub = parse(got[1], got[0])
        items += _earnings(_html_items(sub, got[0], lambda u, t: len(t.split()) >= 3), got[0], domain)

    return _merge(items)


def collect(fetcher, page, final_url, parse, today=None):
    return {
        "profile": profile(page, final_url),
        "tech": tech(page),
        "hiring": hiring(fetcher, page, parse),
        "activity": activity(fetcher, page, final_url, today or date.today()),
    }

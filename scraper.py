"""Scrape a company website: title, links and marketing signals."""
from urllib.parse import urlparse

from fetch import Fetcher, FetchError
from htmlparse import normalize as _normalize, parse, registrable_domain
from signals import collect


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def normalize_input(raw: str) -> str:
    """Accept bare domains ('claramap.com') as well as full URLs."""
    raw = raw.strip()
    url = raw if "://" in raw else "https://" + raw
    try:
        p = urlparse(url)
        ok = p.scheme in ("http", "https") and p.hostname and not any(c.isspace() for c in raw)
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"Invalid domain or URL: {raw[:100]!r}")
    return url


def scrape(url: str, fetcher=None, allow_private=False) -> dict:
    owns = fetcher is None
    fetcher = fetcher or Fetcher(allow_private=allow_private)
    try:
        final, html, _ = fetcher.fetch(url)
        page = parse(html, final)
        signals = collect(fetcher, page, final, parse)
    finally:
        if owns:
            fetcher.close()
    return {
        "url": url,
        "domain": domain_of(final),
        "title": page.title,
        "links": page.links,
        "signals": signals,
        "status": "ok",
    }


__all__ = ["scrape", "normalize_input", "domain_of", "registrable_domain", "FetchError", "_normalize"]

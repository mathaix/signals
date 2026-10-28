"""Web app: scrape one company or a batch of domains; results go to the database."""
import csv
import io
import logging
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from fetch import FetchError
from scraper import normalize_input

log = logging.getLogger(__name__)
MAX_BATCH = 500

PAGE = """<!doctype html><meta charset="utf-8"><title>Scraper</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--ink:#1f2340;--muted:#667;--accent:#db2777;--accent2:#f472b6;--line:#f5d9e8}
*{box-sizing:border-box}
body{font:15px/1.5 system-ui,sans-serif;margin:0;color:var(--ink);background:#fdf4f9}
.hero{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;padding:3rem 1rem 4.5rem;text-align:center}
.hero h1{margin:0;font-size:2.4rem;letter-spacing:-.02em}
.hero p{margin:.5rem auto 0;max-width:36rem;opacity:.9;font-size:1.05rem}
main{max-width:60rem;margin:-3rem auto 3rem;padding:0 1rem}
.card{background:#fff;border:1px solid var(--line);border-radius:1rem;padding:1.25rem 1.5rem;margin-bottom:1.25rem;box-shadow:0 6px 24px rgba(120,20,70,.08)}
.card h2{margin:0 0 .75rem;font-size:1.15rem}
input,textarea{font:inherit;border:1px solid var(--line);border-radius:.6rem;padding:.55rem .75rem;max-width:100%}
input:focus,textarea:focus{outline:2px solid var(--accent);border-color:transparent}
textarea{width:100%}
button{font:inherit;font-weight:600;color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;border-radius:.6rem;padding:.55rem 1.1rem;cursor:pointer;transition:transform .1s,box-shadow .1s}
button:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(91,91,240,.4)}
#url{width:min(100%,28rem)}
label{color:var(--muted);display:inline-block;margin:.5rem 0}
p#status,p#batch-status{color:var(--muted);min-height:1.2em}
.chip{display:inline-block;background:#fce7f3;color:#9d174d;border-radius:1rem;padding:.1rem .7rem;margin:.1rem;font-size:.85rem;font-weight:500}
#result{border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:.6rem;padding:0 1rem;margin:1rem 0;background:#fff7fb}
table{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden;border:1px solid var(--line);border-radius:.6rem}
td,th{padding:.5rem .7rem;text-align:left;border-bottom:1px solid var(--line)}
th{background:#fce7f3;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
tbody tr:nth-child(even){background:#fff7fb} tbody tr:last-child td{border-bottom:0}
a{color:var(--accent)}
</style>
<header class="hero"><h1>Web scraper</h1>
<p>Turn any company domain into marketing signals: tech stack, hiring and activity.</p></header>
<main>
<section class="card">
<h2>Single company</h2>
<form id="f"><input id="url" name="url" placeholder="claramap.com or https://example.com" size="50">
<button id="go">Scrape</button></form>
<p id="status"></p>
<div id="result" hidden></div>
</section>
<section class="card">
<h2>Batch</h2>
<form id="bf"><textarea id="batch" rows="6" cols="50" placeholder="One domain or URL per line (max 500)"></textarea><br>
<label>or CSV (first column) <input type="file" id="csv" accept=".csv,text/csv,text/plain"></label>
<button id="batch-go">Scrape batch</button></form>
<p id="batch-status"></p>
<table id="batch-table" hidden><thead><tr><th>Domain</th><th>Status</th><th>Name</th><th>Tech</th>
<th>Hiring</th><th>Latest post</th></tr></thead><tbody></tbody></table>
</section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const headers = () => ({'Content-Type': 'application/json'});
function el(tag, text, cls, parent) {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (cls) e.className = cls;
  if (parent) parent.appendChild(e);
  return e;
}
async function call(path, opts) {
  const r = await fetch(path, opts);
  let d = {};
  try { d = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(d.detail || 'Request failed');
  return d;
}
function renderResult(d) {
  const box = $('result'), s = d.signals || {}, p = s.profile || {}, h = s.hiring || {}, a = s.activity || {};
  box.replaceChildren();
  el('h3', p.name || d.title || d.domain, null, box).id = 'r-name';
  if (p.description) el('p', p.description, null, box);
  const soc = el('p', 'Social: ', null, box);
  Object.entries(p.socials || {}).filter(([k, v]) => v && /^https?:/.test(v)).forEach(([k, v]) => {
    const l = el('a', k, null, soc); l.href = v; soc.append(' ');
  });
  const t = el('p', 'Tech: ', null, box); t.id = 'r-tech';
  (s.tech || []).forEach((x) => el('span', x, 'chip', t));
  if (!(s.tech || []).length) t.append('none detected');
  el('p', 'Hiring: ' + (h.careers_url ? `${h.careers_url} (ATS: ${h.ats || 'unknown'}, open roles: ${h.open_roles ?? 'n/a'})` : 'no careers page found'), null, box);
  el('p', 'Activity: ' + (a.source ? `latest post ${a.latest_post_date}, ${a.posts_last_90d} in last 90 days (${a.source})` : 'no blog activity found'), null, box);
  box.hidden = false;
}
$('f').onsubmit = async (e) => {
  e.preventDefault();
  const s = $('status');
  s.textContent = 'Scraping...';
  $('result').hidden = true;
  try {
    const d = await call('/scrape', {method: 'POST', headers: headers(), body: JSON.stringify({url: $('url').value})});
    s.textContent = `Saved "${d.title}" with ${d.links.length} links`;
    renderResult(d);
  } catch (err) { s.textContent = 'Error: ' + err.message; }
};
$('csv').onchange = async () => { const f = $('csv').files[0]; if (f) $('batch').value = await f.text(); };
function renderRows(rows) {
  const body = document.querySelector('#batch-table tbody');
  body.replaceChildren();
  rows.forEach((r) => {
    const s = r.signals || {}, tr = el('tr', null, null, body);
    el('td', r.domain || r.url, null, tr);
    el('td', r.status === 'error' ? 'error: ' + r.error : r.status, null, tr);
    el('td', (s.profile || {}).name || '', null, tr);
    el('td', (s.tech || []).join(', '), null, tr);
    const h = s.hiring || {};
    el('td', h.open_roles != null ? h.open_roles + ' roles' : (h.careers_url ? 'careers page' : ''), null, tr);
    el('td', (s.activity || {}).latest_post_date || '', null, tr);
  });
  $('batch-table').hidden = false;
}
async function poll(id) {
  try {
    const d = await call('/batch/' + id, {headers: headers()});
    const c = d.counts;
    $('batch-status').textContent = `queued ${c.queued} · ok ${c.ok} · error ${c.error}`;
    renderRows(d.rows);
    if (c.queued > 0) setTimeout(() => poll(id), 1000);
  } catch (err) { $('batch-status').textContent = 'Error: ' + err.message; }
}
$('bf').onsubmit = async (e) => {
  e.preventDefault();
  $('batch-status').textContent = 'Submitting...';
  try {
    const d = await call('/batch', {method: 'POST', headers: headers(), body: JSON.stringify({text: $('batch').value})});
    $('batch-status').textContent = `Queued ${d.total} items`;
    poll(d.batch_id);
  } catch (err) { $('batch-status').textContent = 'Error: ' + err.message; }
};
</script>"""


class ScrapeRequest(BaseModel):
    url: str


class BatchRequest(BaseModel):
    urls: list[str] | None = None
    text: str | None = None  # one domain/URL per line; CSV allowed (first column is used)


def parse_items(req: BatchRequest) -> list:
    raw = list(req.urls or [])
    if req.text:
        raw += [row[0] for row in csv.reader(io.StringIO(req.text)) if row and row[0].strip()]
    items = []
    for r in raw:
        if r.strip():
            url = normalize_input(r)
            if url not in items:
                items.append(url)
    return items


def create_app(scrape, save, *, spawn=None, queue_batch=None, get_batch=None) -> FastAPI:
    """scrape(url) -> record; save(record);
    spawn(batch_id, [(row_id, url)]), queue_batch(batch_id, urls) -> [{id, url}] and
    get_batch(batch_id) -> rows implement batch processing."""
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.post("/scrape")
    def do_scrape(req: ScrapeRequest):
        try:
            record = scrape(normalize_input(req.url))
        except (FetchError, ValueError) as e:
            raise HTTPException(400, str(e))
        except Exception:
            log.exception("Scrape failed for %r", req.url)
            raise HTTPException(500, "Something went wrong on our side")
        try:
            save(record)
        except Exception:
            log.exception("Saving scrape result failed for %r", req.url)
            raise HTTPException(500, "Something went wrong on our side")
        return record

    @app.post("/batch")
    def do_batch(req: BatchRequest):
        if not (spawn and queue_batch):
            raise HTTPException(503, "Batch processing isn't configured")
        try:
            urls = parse_items(req)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not urls:
            raise HTTPException(400, "No domains or URLs given")
        if len(urls) > MAX_BATCH:
            raise HTTPException(400, f"Too many items ({len(urls)}); the maximum is {MAX_BATCH}")
        batch_id = str(uuid.uuid4())
        try:
            rows = queue_batch(batch_id, urls)
            spawn(batch_id, [(r["id"], r["url"]) for r in rows])
        except Exception:
            log.exception("Starting batch failed")
            raise HTTPException(500, "Something went wrong on our side")
        return {"batch_id": batch_id, "total": len(urls)}

    @app.get("/batch/{batch_id}")
    def batch_status(batch_id: str):
        try:
            uuid.UUID(batch_id)
        except ValueError:
            raise HTTPException(400, "Invalid batch id")
        if not get_batch:
            raise HTTPException(503, "Batch processing isn't configured")
        try:
            rows = get_batch(batch_id)
        except Exception:
            log.exception("Reading batch %s failed", batch_id)
            raise HTTPException(500, "Something went wrong on our side")
        if not rows:
            raise HTTPException(404, "Unknown batch")
        counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("queued", "ok", "error")}
        return {"batch_id": batch_id, "counts": counts, "rows": rows}

    return app

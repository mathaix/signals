import uuid

import pytest
from fastapi.testclient import TestClient

from fetch import FetchError
from jobs import run_job
from web import create_app

class Store:
    def __init__(self):
        self.rows = {}
        self.n = 0

    def save(self, record):
        if "id" in record:
            self.rows[record["id"]].update(record)
        else:
            self.n += 1
            self.rows[self.n] = dict(record, id=self.n)

    def queue(self, batch_id, urls):
        out = []
        for u in urls:
            self.n += 1
            self.rows[self.n] = {"id": self.n, "url": u, "status": "queued", "batch_id": batch_id}
            out.append({"id": self.n, "url": u})
        return out

    def get(self, batch_id):
        return [r for r in self.rows.values() if r.get("batch_id") == batch_id]


def fake_scrape(url):
    if "bad" in url:
        raise FetchError("Couldn't resolve bad.example – check the domain")
    host = url.split("//")[1].strip("/")
    return {"url": url, "domain": host, "title": host, "links": [], "status": "ok",
            "signals": {"profile": {"name": host}, "tech": ["hubspot"]}}


def make_client(scrape=fake_scrape):
    store = Store()

    def spawn(batch_id, jobs):
        for row_id, url in jobs:
            run_job(scrape, store.save, url, batch_id, row_id)

    app = create_app(scrape, store.save, spawn=spawn, queue_batch=store.queue, get_batch=store.get)
    return TestClient(app, raise_server_exceptions=False), store


def test_index():
    c, _ = make_client()
    assert c.get("/").status_code == 200


def test_batch_limits():
    c, _ = make_client()
    r = c.post("/batch", json={"urls": [f"d{i}.com" for i in range(501)]})
    assert r.status_code == 400 and "500" in r.json()["detail"]
    assert c.post("/batch", json={"text": ""}).status_code == 400
    assert c.post("/batch", json={"text": "ok.com\nhas space.com"}).status_code == 400
    assert c.post("/batch", json={"urls": [f"d{i % 500}.com" for i in range(600)]}).status_code == 200


def test_batch_end_to_end():
    c, store = make_client()
    r = c.post("/batch", json={"text": "a.com\nhttps://b.com\nbad.example\na.com\n"})
    assert r.status_code == 200 and r.json()["total"] == 3
    d = c.get(f"/batch/{r.json()['batch_id']}").json()
    assert d["counts"] == {"queued": 0, "ok": 2, "error": 1}
    assert {x["status"] for x in d["rows"]} == {"ok", "error"}
    assert "Couldn't resolve bad.example" in next(x["error"] for x in d["rows"] if x["status"] == "error")


def test_batch_csv_first_column_and_counts_queued():
    store = Store()
    app = create_app(fake_scrape, store.save, spawn=lambda b, j: None, queue_batch=store.queue,
                     get_batch=store.get)
    c = TestClient(app)
    r = c.post("/batch", json={"text": "a.com,Acme\nb.com,Beta"})
    d = c.get(f"/batch/{r.json()['batch_id']}").json()
    assert d["counts"] == {"queued": 2, "ok": 0, "error": 0}
    assert c.get(f"/batch/{uuid.uuid4()}").status_code == 404
    assert c.get("/batch/not-a-uuid").status_code == 400


def test_scrape_errors():
    def boom(url):
        raise RuntimeError("secret internals /etc/passwd")

    c, _ = make_client(boom)
    r = c.post("/scrape", json={"url": "a.com"})
    assert r.status_code == 500 and "passwd" not in r.text
    c, store = make_client()
    assert c.post("/scrape", json={"url": "bad.example"}).status_code == 400
    r = c.post("/scrape", json={"url": "claramap.com"})
    assert r.status_code == 200 and r.json()["url"] == "https://claramap.com" and store.rows


def test_batch_status_counts_children_apart_from_submitted_urls():
    store = Store()
    client = TestClient(create_app(fake_scrape, store.save, get_batch=store.get), raise_server_exceptions=False)
    b = str(uuid.uuid4())
    rows = [(1, None, "ok"), (2, None, "queued"), (3, 1, "queued"), (4, 1, "ok"), (5, 1, "error"), (6, 1, "ok"), (7, 2, "queued")]
    for i, parent, status in rows:
        store.rows[i] = {"id": i, "parent_id": parent, "url": f"https://r{i}.example", "status": status, "batch_id": b}
    d = client.get(f"/batch/{b}").json()
    assert d["counts"] == {"queued": 1, "ok": 1, "error": 0}
    assert d["children"] == {"queued": 2, "ok": 2, "error": 1, "total": 5}
    assert [r["id"] for r in d["rows"]] == [1, 2]
    assert d["rows"][0]["children"] == {"queued": 1, "ok": 2, "error": 1, "total": 4}
    assert d["rows"][1]["children"] == {"queued": 1, "ok": 0, "error": 0, "total": 1}


def test_batch_without_children_reports_zero_children():
    client, store = make_client()
    b = client.post("/batch", json={"urls": ["a.example"]}).json()["batch_id"]
    d = client.get(f"/batch/{b}").json()
    assert d["children"] == {"queued": 0, "ok": 0, "error": 0, "total": 0}

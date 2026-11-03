"""Core behaviour, run against the example site (no network)."""
import json
import sqlite3

import pytest

from adwatch import config, db, fetcher, server, sites, throttle


@pytest.fixture
def client():
    db.conn()
    return server.app.test_client()


def run_queue(site_id="example"):
    """Do the queued jobs of a site's worker synchronously."""
    w = fetcher.worker(site_id)
    while w.queue:
        job = w.queue[0]
        keep = getattr(w, "_" + job[0])(job[1])
        if not keep:
            w.queue.remove(job)
        else:
            break


def test_add_search_needs_only_name_and_url(client):
    r = client.post("/api/searches", json={"name": "Lamps", "url": "https://www.example.com/search?q=lamp"})
    assert r.status_code == 200
    s = db.get_search(r.json["id"])
    assert s["site"] == "example" and s["filters"] == {}
    bad = client.post("/api/searches", json={"name": "X", "url": "https://elsewhere.ie/search"})
    assert bad.status_code == 400 and "example.com" in bad.json["error"]


def test_on_demand_fetches_page_one_then_next_only_when_low(client, monkeypatch):
    calls = []
    orig = sites.get("example").fetch_page
    monkeypatch.setattr(sites.get("example"), "fetch_page", lambda s, n: calls.append(n) or orig(s, n))
    r = client.get("/api/ads?list=market")
    assert r.json["ads"] == [] and r.json["loading"]
    run_queue()
    assert calls == [0]
    ads = client.get("/api/ads?list=market").json
    assert len(ads["ads"]) == 20 and ads["more"]
    run_queue()
    assert calls == [0], "no extra page while plenty of ads are left"
    for a in ads["ads"][:18]:
        client.post(f"/api/ads/{a['id']}/status", json={"status": "disliked"})
    client.get("/api/ads?list=market")
    run_queue()
    assert calls == [0, 1]
    s = db.list_searches()[0]
    assert s["next_page"] == 2 and s["total"] == 45 and not s["exhausted"]
    # last page
    for a in client.get("/api/ads?list=market").json["ads"]:
        client.post(f"/api/ads/{a['id']}/status", json={"status": "liked"})
    client.get("/api/ads?list=market"); run_queue()
    assert calls == [0, 1, 2]
    s = db.list_searches()[0]
    assert s["exhausted"]
    assert client.get("/api/ads?list=market").json["more"] is False


def test_rate_limit_keeps_the_job_and_pauses(client, monkeypatch):
    site = sites.get("example")

    def limited(s, n):
        raise throttle.RateLimited("example asked us to slow down (HTTP 429)")
    monkeypatch.setattr(site, "fetch_page", limited)
    client.get("/api/ads?list=market")
    w = fetcher.worker("example")
    keep = w._first(w.queue[0][1])
    assert keep is True
    assert throttle.blocked_until("example") is not None
    assert "slow down" in db.list_searches()[0]["last_error"]
    throttle.ok("example")


def test_background_mode_reads_all_pages(client, monkeypatch):
    site = sites.get("example")
    monkeypatch.setattr(site, "mode", "background")
    monkeypatch.setattr(site, "max_pages", 5)
    sid = db.list_searches()[0]["id"]
    fetcher.refresh([sid])
    run_queue()
    s = db.get_search(sid)
    assert s["last_count"] == 45 and s["last_error"] is None
    assert len(client.get("/api/ads?list=market").json["ads"]) == 45
    # ads not seen for a long time drop out of the Market for background sites
    db.x("UPDATE ads SET last_seen='2000-01-01T00:00:00Z'")
    assert client.get("/api/ads?list=market").json["ads"] == []


def test_filters_and_exclusions(client):
    sid = db.list_searches()[0]["id"]
    w = fetcher.worker("example")
    w._first(sid)
    all_ads = client.get("/api/ads?list=market").json["ads"]
    client.put(f"/api/searches/{sid}", json={"filters": {"price": {"max": 400}, "bogus": {"max": 1}}})
    assert db.get_search(sid)["filters"] == {"price": {"max": 400}}
    cheap = client.get("/api/ads?list=market").json["ads"]
    assert cheap and all(a["price"] <= 400 for a in cheap) and len(cheap) < len(all_ads)
    county = cheap[0]["county"]
    r = client.post("/api/exclusions", json={"field": "county", "value": county})
    assert r.status_code == 200
    assert all(a["county"] != county for a in client.get("/api/ads?list=market").json["ads"])
    assert client.post("/api/exclusions", json={"field": "title", "value": "x"}).status_code == 400
    client.delete(f"/api/exclusions/{r.json['exclusion']['id']}")
    assert len(client.get("/api/ads?list=market").json["ads"]) == len(cheap)


def test_side_and_distance_filters(client):
    sid = db.list_searches()[0]["id"]
    fetcher.worker("example")._first(sid)
    client.put(f"/api/searches/{sid}", json={"filters": {"side": "west", "distance": {"max": 60}}})
    ads = client.get("/api/ads?list=market").json["ads"]
    centre = config.get()["centre"]
    assert ads and all(a["km"] <= 60 and a["lng"] <= centre["lng"] + 0.05 for a in ads)


def test_saved_ad_recheck(client):
    sid = db.list_searches()[0]["id"]
    fetcher.worker("example")._first(sid)
    ad = client.get("/api/ads?list=market").json["ads"][0]
    client.post(f"/api/ads/{ad['id']}/status", json={"status": "liked"})
    db.x("UPDATE ads SET last_seen='2000-01-01T00:00:00Z', first_seen='2000-01-01T00:00:00Z'")
    assert fetcher.check_tick() == 1
    run_queue()
    assert db.get_ad(ad["id"])["checked_at"]


def test_comparables(client):
    sid = db.list_searches()[0]["id"]
    fetcher.worker("example")._first(sid)
    ads = client.get("/api/ads?list=market").json["ads"]
    assert any(a["comp"] and a["comp"]["count"] >= 3 for a in ads)


def test_page_and_config(client):
    page = client.get("/").data.decode()
    assert "<title>AdWatch</title>" in page and "ADWATCH_CONFIG" in page
    cfg = client.get("/api/config").json
    assert cfg["sites"][0]["id"] == "example"
    assert client.get("/fork.js").status_code == 200
    assert client.get("/healthz").data == b"ok"


def test_upgrade_from_pre_adwatch_database(tmp_path, monkeypatch):
    """Old CarWatch/DaftWatch layout: refinement columns on searches, fields as columns on ads."""
    path = tmp_path / "old.db"
    monkeypatch.setenv("DB_PATH", str(path))
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES('seeded','x');
    CREATE TABLE searches(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, enabled INTEGER DEFAULT 1,
      min_year INTEGER, max_year INTEGER, min_price INTEGER, max_price INTEGER, category TEXT,
      last_run TEXT, last_count INTEGER, last_error TEXT, next_from INTEGER, total INTEGER, refreshed_at TEXT);
    INSERT INTO searches(name, url, min_year, max_price, category, next_from, total, refreshed_at)
      VALUES ('Bikes', 'https://www.example.com/search?q=bike', 2015, 500, 'bike', 40, 45, '2026-01-01T00:00:00Z');
    CREATE TABLE ads(id TEXT PRIMARY KEY, url TEXT, title TEXT, price INTEGER, category TEXT, year INTEGER,
      county TEXT, image TEXT, first_seen TEXT, last_seen TEXT, price_history TEXT, status TEXT, status_at TEXT);
    INSERT INTO ads VALUES ('a1', 'https://www.example.com/ad/1', 'Bike 1', 300, 'Bike', 2018, 'Cork', NULL,
      '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', '[["2026-01-01T00:00:00Z", 350]]', 'liked', '2026-01-02T00:00:00Z');
    CREATE TABLE ad_search(ad_id TEXT, search_id INTEGER, PRIMARY KEY(ad_id, search_id)); INSERT INTO ad_search VALUES ('a1', 1);
    CREATE TABLE exclusions(id INTEGER PRIMARY KEY AUTOINCREMENT, field TEXT, value TEXT, UNIQUE(field, value));
    INSERT INTO exclusions(field, value) VALUES ('county', 'Kerry');
    CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, found INTEGER, new_ads INTEGER, error TEXT);
    """)
    c.commit(); c.close()
    monkeypatch.setitem(config.get(), "default_site", "example")
    db.close()
    s = db.list_searches()[0]
    assert s["site"] == "example" and s["next_page"] == 2 and s["total"] == 45
    assert s["filters"] == {"year": {"min": 2015}, "price": {"max": 500}, "category": {"contains": "bike"}}
    ad = db.get_ad("a1")
    assert ad["site"] == "example" and ad["year"] == 2018 and ad["status"] == "liked"
    assert ad["price_history"] == [["2026-01-01T00:00:00Z", 350]]
    assert db.list_exclusions()[0]["value"] == "Kerry"
    assert (tmp_path / "old.db.pre-adwatch").exists()
    # starting again doesn't upgrade twice
    db.close()
    assert len(db.list_searches()) == 1

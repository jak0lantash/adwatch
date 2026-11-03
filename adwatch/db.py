"""SQLite storage. Ads keep a few built-in columns; everything site-specific is in `attrs` (JSON).

Databases from CarWatch/DaftWatch before adwatch are upgraded in place on first start
(a copy of the old file is kept next to it as <name>.pre-adwatch).
"""
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import config, sites

log = logging.getLogger("db")
_lock = threading.RLock()
_conn = None

CORE = ["site", "url", "title", "price", "price_text", "image", "lat", "lng", "published"]
STATE = ["first_seen", "last_seen", "price_history", "status", "status_at", "checked_at", "gone_at"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS searches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, url TEXT NOT NULL, site TEXT, enabled INTEGER NOT NULL DEFAULT 1,
  filters TEXT NOT NULL DEFAULT '{}',
  last_run TEXT, last_count INTEGER, last_error TEXT,
  next_page INTEGER NOT NULL DEFAULT 0, total INTEGER, exhausted INTEGER NOT NULL DEFAULT 0, refreshed_at TEXT
);
CREATE TABLE IF NOT EXISTS ads(
  id TEXT PRIMARY KEY,
  site TEXT, url TEXT, title TEXT, price INTEGER, price_text TEXT, image TEXT,
  lat REAL, lng REAL, published TEXT,
  attrs TEXT NOT NULL DEFAULT '{}',
  first_seen TEXT, last_seen TEXT, price_history TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'new', status_at TEXT, checked_at TEXT, gone_at TEXT
);
CREATE TABLE IF NOT EXISTS ad_search(ad_id TEXT, search_id INTEGER, PRIMARY KEY(ad_id, search_id));
CREATE TABLE IF NOT EXISTS exclusions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, field TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(field, value)
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, search_id INTEGER, started TEXT, finished TEXT,
  found INTEGER, new_ads INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS ads_status ON ads(status);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def db_path():
    return os.environ.get("DB_PATH") or f"/data/{config.get()['id']}.db"


def conn():
    global _conn
    if _conn is None:
        path = db_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        if _is_legacy(c):
            c = _upgrade_legacy(c, path)
        c.executescript(SCHEMA)
        _seed(c)
        _conn = c
    return _conn


def close():
    """For tests."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def q(sql, args=(), one=False):
    with _lock:
        rows = [dict(r) for r in conn().execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


# ---------- upgrading pre-adwatch databases ----------

def _cols(c, table):
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _is_legacy(c):
    cols = _cols(c, "ads")
    return bool(cols) and "attrs" not in cols


def _legacy_filter_map():
    fields = config.fields()
    mapping = dict(config.get()["legacy_search_columns"])
    return fields, mapping


def _num(v):
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def legacy_filters(row):
    """Old per-column search refinements -> the filters dict."""
    fields, mapping = _legacy_filter_map()
    skip = {"id", "name", "url", "enabled", "last_run", "last_count", "last_error", "next_from",
            "total", "refreshed_at", "source", "site"}
    out = {}
    for col, v in row.items():
        if col in skip or v in (None, "", 0) and col not in mapping:
            continue
        target = mapping.get(col)
        if target is None:
            m = re.fullmatch(r"(min|max)_(.+)", col)
            if m and (m.group(2) in fields or m.group(2) == "price"):
                target = f"{m.group(2)}.{m.group(1)}"
            elif col in fields:
                target = f"{col}.contains"
        if not target or v in (None, ""):
            continue
        if target == "side":
            if v:
                out["side"] = "west" if v in (1, "1", True) else str(v)
            continue
        key, _, kind = target.partition(".")
        val = str(v).strip() if kind == "contains" else _num(v)
        if val in (None, ""):
            continue
        out.setdefault(key, {})[kind] = val
    return out


def _upgrade_legacy(c, path):
    log.warning("upgrading %s from the pre-adwatch layout (backup: %s.pre-adwatch)", path, path)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    backup = path + ".pre-adwatch"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    default_site = config.get()["default_site"]
    with c:
        for t in ("searches", "ads", "runs"):
            if _cols(c, t):
                c.execute(f"ALTER TABLE {t} RENAME TO legacy_{t}")
        c.executescript(SCHEMA)
        # searches
        for r in c.execute("SELECT * FROM legacy_searches").fetchall():
            r = dict(r)
            site = sites.for_url(r["url"])
            page_size = site.page_size if site else 20
            nf = r.get("next_from") or 0
            c.execute("INSERT INTO searches(id, name, url, site, enabled, filters, last_run, last_count, last_error,"
                      " next_page, total, exhausted, refreshed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["id"], r["name"], r["url"], site.id if site else None, r.get("enabled", 1),
                       json.dumps(legacy_filters(r)), r.get("last_run"), r.get("last_count"), r.get("last_error"),
                       math.ceil(nf / page_size), r.get("total"),
                       1 if r.get("total") is not None and nf >= (r.get("total") or 0) else 0, r.get("refreshed_at")))
        # ads
        core = set(CORE) | set(STATE) | {"id", "source"}
        for r in c.execute("SELECT * FROM legacy_ads").fetchall():
            r = dict(r)
            attrs = {k: v for k, v in r.items() if k not in core and v is not None}
            site = r.get("source") or default_site
            c.execute("INSERT INTO ads(id, site, url, title, price, price_text, image, lat, lng, published, attrs,"
                      " first_seen, last_seen, price_history, status, status_at, checked_at, gone_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["id"], site, r.get("url"), r.get("title"), r.get("price"), r.get("price_text"),
                       r.get("image"), r.get("lat"), r.get("lng"), r.get("published"), json.dumps(attrs),
                       r.get("first_seen"), r.get("last_seen"), r.get("price_history") or "[]",
                       r.get("status") or "new", r.get("status_at"), r.get("checked_at"), r.get("gone_at")))
        if _cols(c, "legacy_runs"):
            c.execute("INSERT INTO runs(id, started, finished, found, new_ads, error) "
                      "SELECT id, started, finished, found, new_ads, error FROM legacy_runs")
        for t in ("searches", "ads", "runs"):
            c.execute(f"DROP TABLE IF EXISTS legacy_{t}")
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('seeded', ?)", (now(),))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('upgraded_from_legacy', ?)", (now(),))
    log.warning("upgrade done: %d searches, %d ads",
                c.execute("SELECT COUNT(*) FROM searches").fetchone()[0], c.execute("SELECT COUNT(*) FROM ads").fetchone()[0])
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _seed(c):
    if c.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
        return
    for s in config.get()["default_searches"]:
        site = sites.for_url(s["url"])
        c.execute("INSERT INTO searches(name, url, site, filters) VALUES (?,?,?,?)",
                  (s["name"], s["url"], site.id if site else None, json.dumps(s.get("filters") or {})))
    c.execute("INSERT INTO meta(key, value) VALUES ('seeded', ?)", (now(),))
    c.commit()


# ---------- searches ----------

def _search_row(r):
    r["filters"] = json.loads(r.get("filters") or "{}")
    r["enabled"] = bool(r["enabled"])
    r["exhausted"] = bool(r["exhausted"])
    return r


def list_searches():
    return [_search_row(r) for r in q("SELECT * FROM searches ORDER BY id")]


def get_search(sid):
    r = q("SELECT * FROM searches WHERE id=?", (sid,), one=True)
    return _search_row(r) if r else None


def clean_filters(filters):
    """Keep only filters the app knows, with numbers as numbers."""
    fields = config.fields()
    out = {}
    for key, f in (filters or {}).items():
        if key == "side":
            if f in ("west", "east", "north", "south"):
                out["side"] = f
            continue
        if key not in fields and key not in ("price", "distance"):
            continue
        if not isinstance(f, dict):
            continue
        clean = {}
        for kind, v in f.items():
            if kind == "contains":
                v = str(v or "").strip()
                if v:
                    clean[kind] = v
            elif kind in ("min", "max"):
                n = _num(v)
                if n is not None:
                    clean[kind] = n
        if clean:
            out[key] = clean
    return out


def save_search(data, sid=None):
    vals = {}
    if "name" in data:
        vals["name"] = (data.get("name") or "").strip()
    if "url" in data:
        vals["url"] = (data.get("url") or "").strip()
        site = sites.for_url(vals["url"])
        vals["site"] = site.id if site else None
    if "enabled" in data:
        vals["enabled"] = 1 if data["enabled"] else 0
    if "filters" in data:
        vals["filters"] = json.dumps(clean_filters(data["filters"]))
    if sid:
        old = get_search(sid)
        if vals:
            x(f"UPDATE searches SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), sid))
        if old and "url" in vals and vals["url"] != old["url"]:
            restart_search(sid)
        return sid
    vals.setdefault("enabled", 1)
    vals.setdefault("filters", "{}")
    return x(f"INSERT INTO searches({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))


def restart_search(sid):
    x("UPDATE searches SET next_page=0, total=NULL, exhausted=0, refreshed_at=NULL WHERE id=?", (sid,))


def update_paging(sid, next_page, total, exhausted, refreshed=False, count=None):
    t = now()
    x("UPDATE searches SET next_page=?, total=?, exhausted=?, last_error=NULL, last_run=?, "
      "last_count=COALESCE(?, last_count), refreshed_at=CASE WHEN ? THEN ? ELSE refreshed_at END WHERE id=?",
      (next_page, total, 1 if exhausted else 0, t, count, 1 if refreshed else 0, t, sid))


def search_error(sid, msg):
    x("UPDATE searches SET last_error=?, last_run=? WHERE id=?", (msg, now(), sid))


def delete_search(sid):
    x("DELETE FROM searches WHERE id=?", (sid,))
    x("DELETE FROM ad_search WHERE search_id=?", (sid,))


# ---------- ads ----------

def _split(ad):
    core = {k: ad.get(k) for k in CORE}
    attrs = {k: v for k, v in ad.items() if k not in CORE and k != "id" and k not in STATE}
    return core, attrs


def upsert_ad(ad, search_id, site):
    """Insert or refresh an ad found by a search. Returns True if it's new."""
    t = now()
    core, attrs = _split(dict(ad, site=site.id))
    with _lock:
        c = conn()
        row = c.execute("SELECT price, price_history, attrs FROM ads WHERE id=?", (ad["id"],)).fetchone()
        if row is None:
            hist = [[t, core["price"]]] if core.get("price") else []
            c.execute(f"INSERT INTO ads(id, {', '.join(CORE)}, attrs, first_seen, last_seen, price_history) "
                      f"VALUES (?, {', '.join('?' * len(CORE))}, ?, ?, ?, ?)",
                      (ad["id"], *core.values(), json.dumps({k: v for k, v in attrs.items() if v is not None}),
                       t, t, json.dumps(hist)))
            new = True
        else:
            hist = json.loads(row["price_history"] or "[]")
            if core.get("price") and core["price"] != row["price"]:
                hist.append([t, core["price"]])
            merged = json.loads(row["attrs"] or "{}")
            for k, v in attrs.items():
                if v is not None or k in site.clearable:
                    merged[k] = v
            merged = {k: v for k, v in merged.items() if v is not None}
            c.execute(f"UPDATE ads SET {', '.join(f'{k}=COALESCE(?, {k})' for k in CORE)}, attrs=?, last_seen=?, "
                      "price_history=?, gone_at=NULL WHERE id=?",
                      (*core.values(), json.dumps(merged), t, json.dumps(hist), ad["id"]))
            new = False
        c.execute("INSERT OR IGNORE INTO ad_search(ad_id, search_id) VALUES (?,?)", (ad["id"], search_id))
        c.commit()
    return new


def record_check(ad_id, gone=False, price=None, error=False):
    """Result of rechecking a saved ad on its own page."""
    t = now()
    with _lock:
        c = conn()
        row = c.execute("SELECT price, price_history FROM ads WHERE id=?", (ad_id,)).fetchone()
        if row is None:
            return
        if error:
            c.execute("UPDATE ads SET checked_at=? WHERE id=?", (t, ad_id))
        elif gone:
            c.execute("UPDATE ads SET checked_at=?, gone_at=? WHERE id=?", (t, t, ad_id))
        else:
            hist = json.loads(row["price_history"] or "[]")
            p = row["price"]
            if price and price != row["price"]:
                hist.append([t, price])
                p = price
            c.execute("UPDATE ads SET checked_at=?, last_seen=?, price=?, price_history=?, gone_at=NULL WHERE id=?",
                      (t, t, p, json.dumps(hist), ad_id))
        c.commit()


def set_status(ad_id, status):
    x("UPDATE ads SET status=?, status_at=? WHERE id=?", (status, now(), ad_id))


def _ad_row(r):
    attrs = json.loads(r.pop("attrs") or "{}")
    ad = {**attrs, **r}
    ad["price_history"] = json.loads(ad.get("price_history") or "[]")
    site = sites.get(ad.get("site"))
    if site:
        site.derive(ad)
    return ad


def get_ad(ad_id):
    r = q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)
    return _ad_row(r) if r else None


# ---------- geography ----------

def km_from_centre(lat, lng):
    centre = config.get()["centre"]
    if not centre or lat is None or lng is None:
        return None
    p1, p2 = math.radians(centre["lat"]), math.radians(lat)
    dp, dl = p2 - p1, math.radians(lng - centre["lng"])
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def on_side(ad, side):
    """Is the ad on that side of the centre (with a margin, so the centre town itself counts)?"""
    centre = config.get()["centre"]
    if not centre or ad.get("lat") is None or ad.get("lng") is None:
        return True
    margin = centre.get("margin_km", 2.5)
    east = (ad["lng"] - centre["lng"]) * 111.32 * math.cos(math.radians(centre["lat"]))
    north = (ad["lat"] - centre["lat"]) * 110.57
    return {"west": east <= margin, "east": east >= -margin,
            "north": north >= -margin, "south": north <= margin}.get(side, True)


# ---------- filtering ----------

def _norm_value(field, v):
    f = config.fields().get(field, {})
    if f.get("type") in ("int", "decimal"):
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        if not m:
            return None
        n = float(m.group())
        return f"{n:.{f.get('decimals', 0)}f}"
    return re.sub(r"\s+", " ", str(v).strip().lower())


def matches_filters(ad, filters):
    """Search refinements. A value the ad doesn't have passes."""
    for key, f in filters.items():
        if key == "side":
            if not on_side(ad, f):
                return False
            continue
        v = ad.get("km") if key == "distance" else ad.get(key)
        if v is None or v == "":
            continue
        if "contains" in f:
            alts = [a.strip().lower() for a in f["contains"].split(",") if a.strip()]
            if alts and not any(a in str(v).lower() for a in alts):
                return False
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if "min" in f and n < f["min"]:
            return False
        if "max" in f and n > f["max"]:
            return False
    return True


def excluded(ad, exclusions):
    for e in exclusions:
        v = ad.get(e["field"])
        if v in (None, ""):
            continue
        if _norm_value(e["field"], v) == _norm_value(e["field"], e["value"]):
            return True
    return False


def stale_cutoff(site):
    hours = float(os.environ.get("STALE_HOURS", max(48, site.interval_hours * 3 + 24)))
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ads_for_list(which, search_id=None):
    cfg = config.get()
    searches = {s["id"]: s for s in list_searches()}
    links = {}
    for r in q("SELECT ad_id, search_id FROM ad_search"):
        links.setdefault(r["ad_id"], []).append(r["search_id"])
    exclusions = q("SELECT * FROM exclusions")
    cutoffs = {sid: stale_cutoff(s) for sid, s in sites.load().items() if s.mode == "background"}
    if which == "market":
        rows = q("SELECT * FROM ads WHERE status='new' AND gone_at IS NULL "
                 "ORDER BY COALESCE(published, first_seen) DESC, id")
    else:
        rows = q("SELECT * FROM ads WHERE status=? ORDER BY status_at DESC", ("liked" if which == "liked" else "disliked",))

    out = []
    for r in rows:
        ad = _ad_row(r)
        ad["km"] = km_from_centre(ad.get("lat"), ad.get("lng"))
        # Background sites are re-read on a schedule, so an ad missing for a while has gone.
        # On-demand sites only fetch the pages you reach, so their ads stay until swiped.
        stale = ad["site"] in cutoffs and (ad["last_seen"] or "") < cutoffs[ad["site"]]
        sids = [sid for sid in links.get(ad["id"], []) if sid in searches]
        if which == "market":
            if stale or (ad["price"] is None and cfg["hide_no_price"]) or excluded(ad, exclusions):
                continue
            site = sites.get(ad["site"])
            sids = [sid for sid in sids if searches[sid]["enabled"] and matches_filters(ad, searches[sid]["filters"])
                    and (site is None or site.search_matches(ad, searches[sid]))]
            if not sids or (search_id and search_id not in sids):
                continue
        ad["searches"] = [searches[sid]["name"] for sid in sids]
        ad["search_ids"] = sids
        ad["active"] = ad["gone_at"] is None and not stale
        out.append(ad)
    _add_comparables(out)
    return out


def _add_comparables(ads):
    """Median price (or price per X) of similar ads seen so far, per config "compare"."""
    cmp = config.get()["compare"]
    for ad in ads:
        ad["comp"] = None
    if not cmp:
        return
    group, near, per = cmp.get("group", []), cmp.get("near", {}), cmp.get("per")
    min_per = cmp.get("min_per", 0)

    def value(a):
        if not a.get("price"):
            return None
        if per:
            d = a.get(per)
            try:
                d = float(d)
            except (TypeError, ValueError):
                return None
            return a["price"] / d if d > min_per else None
        return a["price"]

    def key(a):
        k = tuple(str(a.get(g) or "").lower() for g in group)
        return None if any(not p for p in k) else k

    pool = {}
    for r in q("SELECT * FROM ads WHERE price IS NOT NULL"):
        a = _ad_row(r)
        k, v = key(a), value(a)
        if k is not None and v is not None:
            pool.setdefault(k, []).append((a, v))
    for ad in ads:
        k, mine = key(ad), value(ad)
        if k is None or mine is None:
            continue
        vals = []
        for other, v in pool.get(k, []):
            if other["id"] == ad["id"]:
                continue
            try:
                if all(abs(float(other.get(f)) - float(ad.get(f))) <= d for f, d in near.items()):
                    vals.append(v)
            except (TypeError, ValueError):
                continue
        if len(vals) >= cmp.get("min", 3):
            vals.sort()
            n = len(vals)
            med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
            ad["comp"] = {"median": round(med), "count": n, "value": round(mine)}


def counts():
    return {k: len(ads_for_list(k)) for k in ("market", "liked", "disliked")}


# ---------- exclusions ----------

def list_exclusions():
    return q("SELECT * FROM exclusions ORDER BY field, value")


def add_exclusion(field, value):
    f = config.fields().get(field)
    if not f or not f.get("exclude") or value in (None, ""):
        raise ValueError("Can't exclude on that field")
    value = str(value).strip()
    if f.get("type") in ("int", "decimal"):
        value = _norm_value(field, value)
        if value is None:
            raise ValueError("Not a number")
    x("INSERT OR IGNORE INTO exclusions(field, value) VALUES (?,?)", (field, value))
    return q("SELECT * FROM exclusions WHERE field=? AND value=?", (field, value), one=True)


def delete_exclusion(eid):
    x("DELETE FROM exclusions WHERE id=?", (eid,))

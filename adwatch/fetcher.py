"""All requests to websites go through here: one worker thread and one queue per site.

Jobs:
  first <search>   page 1 of an on-demand search (new listings; results are newest first)
  next <search>    the next page of an on-demand search, when the user is running low on ads
  scan <search>    a background run: every page of the search, up to the site's max_pages
  check <ad>       recheck a saved ad on its own page

Each site's requests are spaced by its min_gap; if the site says to slow down, its queue
pauses (5 min, doubling up to an hour) and then carries on where it was.
"""
import json
import logging
import math
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db, sites, throttle

log = logging.getLogger("fetcher")
LOW_WATER = int(os.environ.get("PREFETCH_WHEN_LEFT", "3"))
DEBUG_DIR = os.environ.get("DEBUG_DIR", "/data/debug")
CHECK_SAVED_HOURS = float(os.environ.get("CHECK_SAVED_HOURS", "48"))
CHECK_TICK_MINUTES = float(os.environ.get("CHECK_TICK_MINUTES", "60"))
CHECK_MAX_PER_TICK = int(os.environ.get("CHECK_MAX_PER_TICK", "6"))

checker_state = {"last_checked": None, "error": None}


class Worker:
    def __init__(self, site):
        self.site = site
        self.cv = threading.Condition()
        self.queue = []
        self.state = {"doing": None, "error": None, "last_done": None}

    # ---- queue ----
    def request(self, kind, target):
        with self.cv:
            if (kind, target) not in self.queue:
                self.queue.append((kind, target))
                self.cv.notify()

    def pending(self, search_ids=None, kinds=("first", "next", "scan")):
        with self.cv:
            jobs = list(self.queue)
        doing = self.state.get("job")
        if doing:
            jobs.append(doing)
        return any(k in kinds and (search_ids is None or t in search_ids) for k, t in jobs)

    def status(self):
        with self.cv:
            n = len(self.queue)
        return {**{k: v for k, v in self.state.items() if k != "job"}, "queued": n,
                "blocked_until": throttle.blocked_until(self.site.id), **self.site.describe()}

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"fetch-{self.site.id}").start()

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue[0]
            self.state["job"] = job
            keep = False
            try:
                keep = getattr(self, "_" + job[0])(job[1])
            except Exception:
                log.exception("%s job %s failed", self.site.id, job)
            finally:
                self.state["doing"] = None
                self.state["job"] = None
            if not keep:
                with self.cv:
                    if job in self.queue:
                        self.queue.remove(job)

    # ---- one request, politely ----
    def _get_page(self, fetcher, search, page_no):
        throttle.wait_turn(self.site.id, self.site.min_gap, self.site.jitter)
        try:
            page = fetcher.fetch_page(search, page_no)
        except throttle.RateLimited:
            pause = throttle.slow_down(self.site.id)
            log.warning("%s asked us to slow down; pausing %d s", self.site.id, pause)
            raise
        except Exception:
            throttle.done(self.site.id)
            raise
        throttle.ok(self.site.id)
        if page_no == 0:
            save_debug(search, page)
        return page

    # ---- on-demand pages ----
    def _first(self, sid):
        return self._page(sid, "first")

    def _next(self, sid):
        return self._page(sid, "next")

    def _page(self, sid, kind):
        s = db.get_search(sid)
        if not s or not s["enabled"] or (kind == "next" and s["exhausted"]):
            return False
        page_no = 0 if kind == "first" else s["next_page"]
        self.state["doing"] = f"{s['name']}: page {page_no + 1}"
        try:
            page = self._get_page(self.site, s, page_no)
        except throttle.RateLimited as e:
            self.state["error"] = str(e)
            db.search_error(sid, str(e))
            return True  # keep the job: wait_turn() holds it until the pause is over
        except Exception as e:
            log.exception("%s failed", self.state["doing"])
            self.state["error"] = str(e)[:300]
            db.search_error(sid, str(e)[:300])
            return False
        self.state["error"] = None
        new = sum(db.upsert_ad(ad, sid, self.site) for ad in page.ads)
        more = self.site.page_more(page, page_no)
        if kind == "first":
            next_page = max(s["next_page"], 1)
            if s["next_page"] <= 1:
                exhausted = not more
            else:  # already paged further; only more results overall can reopen it
                grew = page.total is not None and s["total"] is not None and page.total > s["total"]
                exhausted = s["exhausted"] and not grew
        else:
            next_page = page_no + 1
            exhausted = not more
        total = page.total if page.total is not None else s["total"]
        db.update_paging(sid, next_page, total, exhausted, refreshed=(kind == "first"), count=page.total)
        self.state["last_done"] = db.now()
        log.info("%s: %d ads, %d new%s", self.state["doing"], len(page.ads), new, "" if more else " (last page)")
        return False

    # ---- background runs ----
    def _scan(self, sid):
        s = db.get_search(sid)
        if not s or not s["enabled"]:
            return False
        run_id = db.x("INSERT INTO runs(search_id, started) VALUES (?, ?)", (sid, db.now()))
        found = new = 0
        err = None
        seen = set()
        try:
            with self.site.session() as fetcher:
                for page_no in range(self.site.max_pages):
                    self.state["doing"] = f"{s['name']}: page {page_no + 1}"
                    page = self._get_page(fetcher, s, page_no)
                    fresh = [a for a in page.ads if a["id"] not in seen]
                    for ad in fresh:
                        seen.add(ad["id"])
                        new += db.upsert_ad(ad, sid, self.site)
                    found += len(fresh)
                    if not fresh or not self.site.page_more(page, page_no):
                        break
            if not found:
                err = "No ads found on the page. Searches > Show what the scraper saw has details."
        except throttle.RateLimited as e:
            err = str(e)
        except Exception as e:
            log.exception("%s scan failed", s["name"])
            err = str(e)[:300]
        db.x("UPDATE runs SET finished=?, found=?, new_ads=?, error=? WHERE id=?", (db.now(), found, new, err, run_id))
        db.x("UPDATE searches SET last_run=?, last_count=?, last_error=?, refreshed_at=? WHERE id=?",
             (db.now(), found, err, db.now(), sid))
        self.state["error"] = err
        self.state["last_done"] = db.now()
        log.info("%s: scan found %d, %d new%s", s["name"], found, new, f" ({err})" if err else "")
        return False

    # ---- saved-ad rechecks ----
    def _check(self, ad_id):
        ad = db.get_ad(ad_id)
        if not ad or ad["status"] != "liked" or ad["gone_at"]:
            return False
        self.state["doing"] = f"rechecking a saved {ad.get('title') or 'ad'}"[:80]
        throttle.wait_turn(self.site.id, self.site.min_gap, self.site.jitter)
        try:
            res = self.site.check_ad(ad)
        except throttle.RateLimited as e:
            throttle.slow_down(self.site.id)
            checker_state["error"] = str(e)
            return False  # try again at the next tick
        except Exception as e:
            throttle.done(self.site.id)
            log.exception("checking %s failed", ad_id)
            checker_state["error"] = f"{ad_id}: {str(e)[:200]}"
            db.record_check(ad_id, error=True)
            return False
        throttle.ok(self.site.id)
        db.record_check(ad_id, gone=res.gone, price=res.price)
        checker_state.update(last_checked=db.now(), error=None)
        log.info("checked %s: gone=%s price=%s %s", ad_id, res.gone, res.price, res.state)
        return False


_workers = {}


def workers():
    if not _workers:
        for s in sites.load().values():
            _workers[s.id] = Worker(s)
    return _workers


def worker(site_id):
    return workers().get(site_id)


def save_debug(search, page):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        base = os.path.join(DEBUG_DIR, f"search-{search['id']}")
        with open(base + ".json", "w") as f:
            json.dump({"search": search["name"], "url": search["url"], "fetched": db.now(),
                       "parsed_count": len(page.ads), "total": page.total, "parsed_ads": page.ads[:5],
                       "screenshot": bool(page.screenshot), **page.debug}, f, indent=1, default=str)
        if page.screenshot:
            with open(base + ".png", "wb") as f:
                f.write(page.screenshot)
    except Exception:
        log.exception("could not save debug info")


# ---------- planning ----------

def _stale(iso, hours):
    return not iso or datetime.now(timezone.utc) - db.ts(iso) > timedelta(hours=hours)


def plan(scope, market_ads):
    """The user is looking at the Market for these searches: queue the pages they'll need."""
    ws = workers()
    for s in scope:
        w = ws.get(s["site"])
        if w and w.site.mode == "on_demand" and _stale(s["refreshed_at"], w.site.refresh_hours):
            w.request("first", s["id"])
    if len(market_ads) >= LOW_WATER:
        return
    candidates = [s for s in scope if (w := ws.get(s["site"])) and w.site.mode == "on_demand"
                  and s["refreshed_at"] and not s["exhausted"]]
    for s in candidates:
        if ws[s["site"]].pending([c["id"] for c in candidates], kinds=("next",)):
            return  # one extra page at a time
    if candidates:
        left = {s["id"]: sum(s["id"] in a["search_ids"] for a in market_ads) for s in candidates}
        s = min(candidates, key=lambda s: (left[s["id"]], s["id"]))
        ws[s["site"]].request("next", s["id"])


def refresh(search_ids=None):
    """"Look for new ads now": page 1 (on demand) or a full run (background)."""
    for s in db.list_searches():
        if search_ids is not None and s["id"] not in search_ids:
            continue
        if search_ids is None and not s["enabled"]:
            continue
        w = worker(s["site"])
        if w:
            w.request("first" if w.site.mode == "on_demand" else "scan", s["id"])


def more_available(scope):
    ws = workers()
    return any((w := ws.get(s["site"])) and w.site.mode == "on_demand" and not s["exhausted"] for s in scope)


def loading(scope):
    ws = workers()
    by_site = {}
    for s in scope:
        by_site.setdefault(s["site"], []).append(s["id"])
    return any(ws[site].pending(ids) for site, ids in by_site.items() if site in ws)


# ---------- schedules ----------

def _background_loop():
    time.sleep(20)
    while True:
        try:
            for s in db.list_searches():
                w = worker(s["site"])
                if s["enabled"] and w and w.site.mode == "background" and \
                        _stale(s["refreshed_at"], w.site.interval_hours * random.uniform(0.9, 1.1)):
                    w.request("scan", s["id"])
        except Exception:
            log.exception("background scheduling failed")
        time.sleep(300)


def due_saved_ads():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CHECK_SAVED_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.q("SELECT id, site FROM ads WHERE status='liked' AND gone_at IS NULL "
                "AND MAX(COALESCE(checked_at, ''), COALESCE(last_seen, ''), first_seen) < ? "
                "ORDER BY MAX(COALESCE(checked_at, ''), COALESCE(last_seen, ''), first_seen)", (cutoff,))


def check_tick():
    """Queue the saved ads that are due a recheck, spread so each is checked about once per period."""
    ws = workers()
    saved = db.q("SELECT COUNT(*) n FROM ads WHERE status='liked' AND gone_at IS NULL", one=True)["n"]
    due = [a for a in due_saved_ads() if (w := ws.get(a["site"])) and w.site.can_check]
    if not due:
        return 0
    per_tick = max(1, math.ceil(saved / max(1.0, CHECK_SAVED_HOURS * 60 / CHECK_TICK_MINUTES)))
    n = min(CHECK_MAX_PER_TICK, len(due), per_tick)
    for a in due[:n]:
        ws[a["site"]].request("check", a["id"])
    return n


def _check_loop():
    time.sleep(120)
    while True:
        try:
            check_tick()
        except Exception:
            log.exception("saved-ad check failed")
        time.sleep(CHECK_TICK_MINUTES * 60 * random.uniform(0.9, 1.1))


def start():
    for w in workers().values():
        w.start()
    if any(w.site.mode == "background" for w in workers().values()):
        threading.Thread(target=_background_loop, daemon=True, name="background").start()
    if any(w.site.can_check for w in workers().values()):
        threading.Thread(target=_check_loop, daemon=True, name="checker").start()

import base64
import glob
import hashlib
import json
import logging
import os
import random
import time
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import config, db, fetcher, sites

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")
PASSWORD = os.environ.get("APP_PASSWORD", "")
DEMO = os.environ.get("DEMO") == "1"

app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.before_request
def auth():
    if not PASSWORD or request.path == "/healthz":
        return None
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            _, pw = base64.b64decode(header[6:]).decode().split(":", 1)
            if pw == PASSWORD:
                return None
        except Exception:
            pass
    return Response("Login required", 401, {"WWW-Authenticate": f'Basic realm="{config.get()["name"]}"'})


# ---------- page ----------

@app.get("/")
def index():
    cfg = config.get()
    with open(os.path.join(app.static_folder, "index.html")) as f:
        html = f.read()
    icon = cfg["icon"]
    html = (html.replace("{{name}}", cfg["name"]).replace("{{icon}}", icon)
            .replace("{{config}}", json.dumps(public_config()).replace("</", "<\\/")))
    return Response(html, mimetype="text/html")


@app.get("/fork.<ext>")
def fork_static(ext):
    """The fork's optional extra script and styles (empty if it has none)."""
    if ext not in ("js", "css"):
        return Response("", 404)
    path = os.path.join(config.fork_dir(), "static", f"fork.{ext}")
    mime = "text/javascript" if ext == "js" else "text/css"
    if not os.path.exists(path):
        return Response("", mimetype=mime)
    return send_file(path, mimetype=mime, max_age=300)


def public_config():
    cfg = config.get()
    keys = ("id", "name", "icon", "item", "items", "share_title", "theme", "fields", "card", "compare", "centre")
    return {**{k: cfg[k] for k in keys}, "sites": [s.describe() for s in sites.load().values()],
            "low_water": fetcher.LOW_WATER, "check_saved_hours": fetcher.CHECK_SAVED_HOURS, "demo": DEMO}


@app.get("/api/config")
def get_config():
    return jsonify(public_config())


@app.get("/healthz")
def healthz():
    db.q("SELECT 1")
    return "ok"


# ---------- ads ----------

@app.get("/api/ads")
def get_ads():
    """The ads in a list. For the Market, also queues the pages you're about to need."""
    which = request.args.get("list", "market")
    if which not in ("market", "liked", "disliked"):
        return jsonify(error="Unknown list"), 400
    sid = request.args.get("search_id", type=int)
    ads = db.ads_for_list(which, search_id=sid)
    out = {"ads": ads}
    if which == "market":
        scope = [s for s in db.list_searches() if s["enabled"] and (not sid or s["id"] == sid)]
        if not DEMO:
            fetcher.plan(scope, ads)
        out["more"] = not DEMO and fetcher.more_available(scope)
        out["loading"] = not DEMO and fetcher.loading(scope)
    return jsonify(out)


@app.post("/api/ads/<ad_id>/status")
def set_status(ad_id):
    status = (request.json or {}).get("status")
    if status not in ("new", "liked", "disliked"):
        return jsonify(error="Status must be new, liked or disliked"), 400
    db.set_status(ad_id, status)
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    searches = db.list_searches()
    refreshed = [s["refreshed_at"] for s in searches if s["refreshed_at"]]
    return jsonify(counts=db.counts(), sites={k: w.status() for k, w in fetcher.workers().items()},
                   last_refresh=max(refreshed, default=None), checker=fetcher.checker_state, demo=DEMO)


@app.post("/api/refresh")
def refresh():
    """Look for new ads now (one search, or every enabled one)."""
    if DEMO:
        return jsonify(error="Demo mode: fetching is switched off"), 403
    sid = (request.json or {}).get("search_id") if request.is_json else None
    fetcher.refresh([sid] if sid else None)
    return jsonify(ok=True)


# ---------- searches ----------

@app.get("/api/searches")
def searches():
    return jsonify(db.list_searches())


@app.post("/api/searches")
def add_search():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    problem = sites.url_problem(url)
    if problem:
        return jsonify(error=problem), 400
    if not (data.get("name") or "").strip():
        return jsonify(error="Give the search a name"), 400
    sid = db.save_search({"name": data["name"], "url": url, "filters": data.get("filters") or {}})
    return jsonify(id=sid)


@app.put("/api/searches/<int:sid>")
def edit_search(sid):
    data = request.json or {}
    if "url" in data:
        problem = sites.url_problem(data.get("url"))
        if problem:
            return jsonify(error=problem), 400
    if "name" in data and not (data.get("name") or "").strip():
        return jsonify(error="Give the search a name"), 400
    db.save_search(data, sid)
    return jsonify(ok=True)


@app.delete("/api/searches/<int:sid>")
def remove_search(sid):
    db.delete_search(sid)
    return jsonify(ok=True)


# ---------- exclusions ----------

@app.get("/api/exclusions")
def exclusions():
    return jsonify(db.list_exclusions())


@app.post("/api/exclusions")
def add_exclusion():
    data = request.json or {}
    try:
        e = db.add_exclusion(data.get("field"), data.get("value"))
    except ValueError as err:
        return jsonify(error=str(err)), 400
    return jsonify(ok=True, exclusion=e)


@app.delete("/api/exclusions/<int:eid>")
def remove_exclusion(eid):
    db.delete_exclusion(eid)
    return jsonify(ok=True)


# ---------- photos that only load through the app ----------

IMG_KEEP_DAYS = 45


def _img_dir():
    return os.path.join(os.path.dirname(db.db_path()) or ".", "imgcache")


def _prune_images(d):
    cutoff = time.time() - IMG_KEEP_DAYS * 86400
    for name in os.listdir(d):
        try:
            if os.path.getmtime(os.path.join(d, name)) < cutoff:
                os.remove(os.path.join(d, name))
        except OSError:
            pass


@app.get("/img")
def img():
    """Some sites' photos don't load when shown on another site: fetch once, cache, serve."""
    url = request.args.get("u", "")
    u = urlparse(url)
    site = next((s for s in sites.load().values() if u.hostname in s.photo_hosts), None)
    if u.scheme != "https" or not site:
        return jsonify(error="Not a photo this app serves"), 400
    d = _img_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, hashlib.sha256(url.encode()).hexdigest())
    if not os.path.exists(path):
        try:
            status, ctype, body = site.fetch_photo(url)
        except Exception as e:
            return jsonify(error=f"Couldn't fetch the photo: {e}"), 502
        if status != 200 or not ctype.startswith("image/") or not body:
            return jsonify(error=f"{site.label} answered HTTP {status}"), 502
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        if random.random() < 0.02:
            _prune_images(d)
    return send_file(path, mimetype="image/jpeg", max_age=30 * 86400)


# ---------- what the scraper saw ----------

@app.get("/api/debug")
def debug():
    out = []
    for path in sorted(glob.glob(os.path.join(fetcher.DEBUG_DIR, "search-*.json"))):
        with open(path) as f:
            item = json.load(f)
        item["name"] = os.path.basename(path)[:-5]
        out.append(item)
    return jsonify(out)


@app.get("/api/debug/<name>.png")
def debug_png(name):
    return send_from_directory(fetcher.DEBUG_DIR, f"{os.path.basename(name)}.png")


def start():
    db.conn()
    if DEMO:
        from .demo import seed
        seed()
    elif os.environ.get("FETCH", "1") == "1":
        fetcher.start()
    port = int(os.environ.get("PORT", "8080"))
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    start()

"""The app's configuration: fork/config.json merged over these defaults.

A fork describes itself entirely in its own folder (fork/ in the repo, /srv/fork in the image):
  fork/config.json     name, look, the fields its ads have, what cards show, filters
  fork/sites/*.py      one plug-in per website (see adwatch/sites.py)
  fork/static/         optional fork.js / fork.css (extra formatters and styles)
  fork/requirements.txt, fork/build.env, fork/tests/   optional
The core repo itself has no fork/ folder and runs the example/ folder instead.
"""
import copy
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fork_dir():
    d = os.environ.get("FORK_DIR")
    if d:
        return d
    for name in ("fork", "example"):
        p = os.path.join(ROOT, name)
        if os.path.isdir(p):
            return p
    raise RuntimeError("No fork/ or example/ folder found")


DEFAULTS = {
    "id": "adwatch",            # also the default database name: /data/<id>.db
    "name": "AdWatch",
    "icon": "🔎",
    "item": "ad", "items": "ads",
    "share_title": "Ads I've saved",
    "theme": {},                # CSS variables, e.g. {"--sticker": "#f4ce3b"}
    "hide_no_price": False,     # hide "price on application" ads from the Market
    "default_site": None,       # site id for ads migrated from a database without one
    # Fields an ad can have, besides the built-in ones (title, price, url, image, lat/lng, ...).
    #   key, label, type: text | int | decimal, decimals, unit, prefix
    #   grouping: false -> no thousands separator (years)
    #   format: name of a formatter in the page (built-in: eur, km, ago; forks add more in fork.js)
    #   exclude: true -> thumbs-down button ("never show ads with this value")
    #   filter: range | min | max | contains -> optional refinement on each search
    "fields": [],
    # What cards show. Templates: {field} is replaced by the field's value; text in [brackets]
    # is dropped when any field inside is missing.
    "card": {
        "heading": "{title}",
        "subtitle": "{title}",
        "sticker": "",
        "badge": None,          # field shown as a ribbon on the photo (e.g. "Sale agreed")
        "rows": [],             # field keys, plus: distance, site, searches, published, first_seen
        "list_meta": "",
        "share": "{price} · {title}",
    },
    # Price comparison with similar ads seen so far (or null):
    #   group: fields that must match; near: {field: max difference}; per: divide price by a field
    "compare": None,
    # Distance filters/rows, measured from here (or null). Env CENTRE_NAME/LAT/LNG override.
    "centre": None,
    # Searches created on first start (name, url, optional filters).
    "default_searches": [],
    # Upgrading a database from before adwatch: old search columns -> filters.
    #   {"max_km": "mileage_km.max", "west_only": "side"}; min_X / max_X map to field X by default.
    "legacy_search_columns": {},
}

_cfg = None


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get():
    global _cfg
    if _cfg is None:
        path = os.path.join(fork_dir(), "config.json")
        with open(path) as f:
            cfg = _merge(DEFAULTS, json.load(f))
        if os.environ.get("CENTRE_LAT") and os.environ.get("CENTRE_LNG"):
            cfg["centre"] = {"name": os.environ.get("CENTRE_NAME", "the centre"),
                             "lat": float(os.environ["CENTRE_LAT"]), "lng": float(os.environ["CENTRE_LNG"])}
        if cfg["centre"]:
            cfg["centre"].setdefault("margin_km", float(os.environ.get("SIDE_MARGIN_KM", "2.5")))
        if os.environ.get("HIDE_POA") in ("0", "1"):
            cfg["hide_no_price"] = os.environ["HIDE_POA"] == "1"
        _cfg = cfg
    return _cfg


def reset():
    """For tests."""
    global _cfg
    _cfg = None


def fields():
    return {f["key"]: f for f in get()["fields"]}

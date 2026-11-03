"""Website plug-ins. Each fork puts one module per site in fork/sites/, defining a Site subclass.

A plug-in only has to read a search results page:

    from adwatch.sites import Site, Page

    class Daft(Site):
        id = "daft"                  # stored with each ad
        label = "daft.ie"            # shown to the user
        hosts = ["daft.ie"]          # a search URL on these hosts belongs to this site
        mode = "on_demand"           # or "background" (see below)
        page_size = 20

        def fetch_page(self, search, page_no):     # page_no = 0, 1, 2 ...
            ...one request...
            return Page(ads=[{...}, ...], total=123)

Ads are dicts. Built-in keys: id (unique, required), url, title, price (int, euro),
price_text, image, lat, lng, published (ISO time). Any other key is stored as a field
(declare it in config.json "fields" to show, filter or thumbs-down on it).

Modes:
  on_demand   Page 1 when the user opens the Market (again after refresh_hours), the next page
              only when they're down to their last few ads. For sites that rate-limit.
  background  Every interval_hours, each search is read up to max_pages pages. Ads not seen
              for a while are treated as gone. For sites that don't mind.

Optional extras: check_ad() to recheck saved ads, photo_hosts + fetch_photo() to serve photos
that won't load on another site, session() to share an expensive setup (a browser) across
the pages of one background run, search_matches() for site-specific result filtering,
derive() to compute extra fields, demo_ads() for DEMO=1.
"""
import contextlib
import glob
import importlib.util
import inspect
import logging
import os
from urllib.parse import urlparse

from . import config, http

log = logging.getLogger("sites")


class Page:
    def __init__(self, ads, total=None, more=None, raw_count=None, debug=None, screenshot=None):
        self.ads = ads              # parsed ads
        self.total = total          # total results the site reports, if it does
        self.more = more            # is there a next page? None = work it out
        self.raw_count = raw_count  # listings on the page before parsing (default: len(ads))
        self.debug = debug or {}    # anything useful for "Show what the scraper saw"
        self.screenshot = screenshot  # PNG bytes, if the site was read in a browser


class Check:
    """Result of rechecking one saved ad on its own page."""

    def __init__(self, gone, price=None, state=""):
        self.gone, self.price, self.state = gone, price, state


class Site:
    id = ""
    label = ""
    hosts = []
    mode = "on_demand"
    page_size = 20
    min_gap = 20.0            # seconds between requests (+ up to `jitter`)
    jitter = 5.0
    refresh_hours = 6.0       # on_demand: re-check page 1 after this long
    interval_hours = 6.0      # background: read every search this often
    max_pages = 5             # background: pages per search per run
    url_hint = ""             # example search address, shown as a placeholder
    photo_hosts = []          # photos on these hosts are served through the app
    clearable = ()            # fields where a missing value clears the old one (e.g. a status)

    # ---- required ----
    def fetch_page(self, search, page_no):
        raise NotImplementedError

    # ---- optional ----
    def check_url(self, url):
        """None if `url` is a search this site can read, else a message for the user."""
        return None

    @contextlib.contextmanager
    def session(self):
        """Background runs fetch all pages of a search through the object yielded here."""
        yield self

    def check_ad(self, ad):
        """Recheck a saved ad: return Check(...). Not implemented = saved ads aren't rechecked."""
        raise NotImplementedError

    def fetch_photo(self, url):
        return http.get_bytes(self.id, url, referer=f"https://www.{self.hosts[0]}/" if self.hosts else None)

    def search_matches(self, ad, search):
        """Site-specific test of whether an ad really belongs to a search (default: yes)."""
        return True

    def derive(self, ad):
        """Add computed fields to an ad (called whenever ads are listed). Mutate `ad`."""

    def demo_ads(self):
        return []

    # ---- helpers ----
    @property
    def can_check(self):
        return type(self).check_ad is not Site.check_ad

    def owns(self, url):
        host = (urlparse(url or "").netloc or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def page_more(self, page, page_no):
        if page.more is not None:
            return page.more
        n = page.raw_count if page.raw_count is not None else len(page.ads)
        if n == 0:
            return False
        if page.total is not None:
            return (page_no + 1) * self.page_size < page.total
        return n >= self.page_size

    def describe(self):
        return {"id": self.id, "label": self.label, "mode": self.mode, "hosts": self.hosts,
                "url_hint": self.url_hint, "refresh_hours": self.refresh_hours,
                "interval_hours": self.interval_hours, "min_gap": self.min_gap,
                "can_check": self.can_check, "photo_hosts": self.photo_hosts}


_sites = None


def _env_override(site):
    """Per-site settings from the environment, e.g. DAFT_MIN_GAP_SECONDS=30."""
    p = site.id.upper().replace("-", "_") + "_"
    for attr, env, cast in (("min_gap", "MIN_GAP_SECONDS", float), ("refresh_hours", "REFRESH_HOURS", float),
                            ("interval_hours", "INTERVAL_HOURS", float), ("max_pages", "MAX_PAGES", int),
                            ("mode", "MODE", str)):
        v = os.environ.get(p + env) or (os.environ.get(env) if attr != "mode" else None)
        if v:
            setattr(site, attr, cast(v))


def load():
    global _sites
    if _sites is None:
        found = {}
        for path in sorted(glob.glob(os.path.join(config.fork_dir(), "sites", "*.py"))):
            name = "fork_sites_" + os.path.basename(path)[:-3]
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if issubclass(cls, Site) and cls is not Site and cls.id and cls.__module__ == name:
                    site = cls()
                    _env_override(site)
                    found[site.id] = site
        if not found:
            raise RuntimeError(f"No site plug-ins in {config.fork_dir()}/sites")
        _sites = found
        log.info("sites: %s", ", ".join(f"{s.id} ({s.mode})" for s in found.values()))
    return _sites


def reset():
    global _sites
    _sites = None


def get(site_id):
    return load().get(site_id)


def for_url(url):
    for s in load().values():
        if s.owns(url):
            return s
    return None


def url_problem(url):
    """None if some site can read this search URL, else a message."""
    site = for_url(url)
    if not site:
        names = " or ".join(s.label for s in load().values())
        return f"Paste a {names} search address"
    return site.check_url(url)

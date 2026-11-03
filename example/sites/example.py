"""A made-up site, so the core runs and can be tested without touching the internet.

Search URLs on example.com return generated ads: ?q=<word> picks the category and
&total=<n> the number of results (default 45). Copy this file to start a new plug-in.
"""
import hashlib
from urllib.parse import parse_qs, urlparse

from adwatch.sites import Check, Page, Site

COUNTIES = ["Cork", "Kerry", "Limerick", "Waterford", "Clare"]
CONDITIONS = ["New", "Like new", "Good", "Fair"]


def _n(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


class Example(Site):
    id = "example"
    label = "example.com"
    hosts = ["example.com"]
    mode = "on_demand"
    page_size = 20
    min_gap = 0.0
    jitter = 0.0
    url_hint = "https://www.example.com/search?q=bike"

    def fetch_page(self, search, page_no):
        qs = parse_qs(urlparse(search["url"]).query)
        cat = (qs.get("q") or ["thing"])[0]
        total = int((qs.get("total") or ["45"])[0])
        ads = [self._ad(cat, i) for i in range(page_no * self.page_size, min(total, (page_no + 1) * self.page_size))]
        return Page(ads, total=total, debug={"title": f"Example: {total} {cat}s", "raw": ads[:2]})

    def _ad(self, cat, i):
        n = _n(cat, i)
        return {
            "id": f"ex-{cat}-{i}",
            "url": f"https://www.example.com/ad/{cat}-{i}",
            "title": f"{cat.capitalize()} #{i}",
            "price": 50 + n % 950,
            "category": cat.capitalize(),
            "condition": CONDITIONS[n % len(CONDITIONS)],
            "year": 2010 + n % 15,
            "county": COUNTIES[n % len(COUNTIES)],
            "lat": 51.6 + (n % 600) / 1000, "lng": -9.2 + (n % 900) / 1000,
        }

    def check_ad(self, ad):
        return Check(gone=False, price=ad.get("price"), state="live")

    def demo_ads(self):
        return [self._ad(c, i) for c in ("bike", "lamp") for i in range(12)]

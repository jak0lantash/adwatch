"""DEMO=1: fill the database with each site's made-up ads (Site.demo_ads) to try the interface.
Nothing is fetched from any website in demo mode."""
from . import db, sites


def seed():
    if db.q("SELECT 1 FROM ads LIMIT 1"):
        return
    for site in sites.load().values():
        ads = site.demo_ads()
        if not ads:
            continue
        sid = next((s["id"] for s in db.list_searches() if s["site"] == site.id), None)
        if sid is None:
            host = site.hosts[0] if site.hosts else "example.com"
            sid = db.save_search({"name": f"{site.label} (demo)", "url": f"https://www.{host}/demo"})
        for ad in ads:
            db.upsert_ad(ad, sid, site)

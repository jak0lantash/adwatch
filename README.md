# AdWatch

Swipe through the results of your saved classified-ad searches. AdWatch gathers the ads that
match several searches on sites like Carzone, DoneDeal or daft.ie into one deck: swipe right
to save, left to hide, and tap the thumbs-down beside any detail (a make, a town, a BER) to
never see ads with it again. Saved ads can be shared as a list of links.

This repo is the **core**. Apps are **forks** of it:

- [CarWatch](https://github.com/jak0lantash/carwatch): Carzone and DoneDeal cars
- [DaftWatch](https://github.com/jak0lantash/daftwatch): daft.ie property

## How forks work

A fork is a copy of this repo plus one folder, `fork/`, that the core never contains:

```
adwatch/            the core: server, database, fetching, the whole front end
example/            a made-up site, used when there's no fork/ (to try and test the core)
fork/               the fork's own folder
  config.json       name, look, the fields its ads have, what cards show
  sites/*.py        one plug-in per website
  static/fork.js    optional: extra value formatters for cards
  static/fork.css   optional: extra styles
  requirements.txt  optional: extra Python packages
  build.env         optional: BASE_IMAGE=... (e.g. a Playwright image for browser-read sites)
  tests/            optional: the fork's tests (run with the core's)
  README.md         the fork's own notes
```

Because a fork only adds files under `fork/`, merging a newer core into it never conflicts.
Improve the core here, then in each fork run `scripts/sync-upstream.sh` (it merges this repo's
`main` and runs the tests). Each fork also has a weekly **Core updates** workflow that opens an
issue when the core has commits it hasn't merged yet.

To start a new fork: create an empty repo, push this repo's `main` to it, add `fork/`
(copy `example/` as a starting point), push. The **Test and build** workflow then tests it and
publishes `ghcr.io/<owner>/<repo>:sha-<commit>`.

## Adding a search

In the app, **Searches → New search**: a name and the address of a search you set up on the
website. The site is recognised from the address. Optional refinements (price, year, distance
from a centre, direction...) are under *Refine further* for things the site can't filter on.

## Two ways of fetching

Each site plug-in picks one:

- **on_demand**, for sites that rate-limit: page 1 of a search when you open the Market (again
  after `refresh_hours`), and the next page only when you're down to your last few ads.
  Results are read newest first, so new listings always land on page 1.
- **background**, for sites that don't mind: every `interval_hours`, every page of each search
  up to `max_pages`. Ads not seen for a while drop out as sold/removed.

Either way, requests to a site go one at a time, at least `min_gap` seconds apart. If a site
says to slow down (HTTP 429/403), its queue pauses for 5 minutes, doubling up to an hour, and
carries on. Saved ads can also be rechecked on their own page every couple of days
(`check_ad`), to catch price drops and sold ads.

## Writing a site plug-in

```python
from adwatch import http
from adwatch.sites import Check, Page, Site

class Daft(Site):
    id = "daft"; label = "daft.ie"; hosts = ["daft.ie"]
    mode = "on_demand"; page_size = 20; min_gap = 20
    url_hint = "https://www.daft.ie/property-for-sale/cork"

    def fetch_page(self, search, page_no):          # one request
        status, _, html = http.get(self.id, build_url(search["url"], page_no))
        data = http.next_data(html)                  # Next.js sites embed their data
        return Page(ads=[parse(x) for x in ...], total=data[...]["totalResults"])
```

An ad is a dict with `id` (unique), `url`, `title`, `price` (euro), and optionally `image`,
`price_text`, `lat`/`lng`, `published`; any other key is a field. Optional methods:
`check_ad`, `session` (share a browser across a background run's pages), `search_matches`,
`derive` (computed fields), `fetch_photo` + `photo_hosts` (serve photos that won't load on
another site), `demo_ads` (for `DEMO=1`). See `adwatch/sites.py` and `example/sites/example.py`.

`adwatch.http` gives plug-ins a client with Chrome's TLS fingerprint, which many sites require.

## config.json

See `adwatch/config.py` for every key and `example/config.json` for a small one. In short:

- `fields`: `key`, `label`, `type` (text/int/decimal), `unit`, `format` (eur, km, ago, or one a
  fork adds in `fork.js`), `exclude` (thumbs-down), `filter` (range/min/max/contains)
- `card`: templates for the heading, sticker, list line and share text, e.g.
  `"{year} {make} {model}"`; text in `[brackets]` is dropped when a field inside is missing.
  `rows` lists the fields shown on a card (plus `distance`, `searches`, `published`, `first_seen`).
- `compare`: "x% below the median of similar ads seen so far" (`group`, `near`, `per`)
- `centre`: distances and the west/east/north/south filter are measured from here
- `default_searches`, `theme`, `hide_no_price`, `legacy_search_columns` (upgrading old databases)

## Running

```
pip install -r requirements.txt
python3 -m adwatch.server          # http://localhost:8080 (example site, no internet needed)
DEMO=1 python3 -m adwatch.server   # made-up ads from each site's demo_ads()
python3 -m pytest -q
```

Environment: `DB_PATH` (default `/data/<id>.db`), `PORT`, `APP_PASSWORD`, `DEMO`, `FETCH=0`,
`PREFETCH_WHEN_LEFT`, `CHECK_SAVED_HOURS`, `STALE_HOURS`, `CENTRE_NAME/LAT/LNG`, and per site
`<SITE>_MIN_GAP_SECONDS`, `_REFRESH_HOURS`, `_INTERVAL_HOURS`, `_MAX_PAGES`, `_MODE`.
`docker-compose.example.yml` runs a fork's image.

Keep requests infrequent and use this for your own searches; each website's terms apply.

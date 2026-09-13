#!/usr/bin/env python3
"""PAK'nSAVE API client + price lookup. Guest token, no login, no browser.

  ./tools/pns.py stores taupo          # find store ids
  ./tools/pns.py price butter milk     # price named items at the default store
  cook shopping-list r.cook --ingredients-only | ./tools/pns.py price -

Stores live in config/paknsave.stores, "<uuid>  <label>" per line; first is the default.
$PNS_STORE overrides. Used as a module by pns_db.py.
"""
import json, os, sys, urllib.request
from pathlib import Path

WEB = "https://www.paknsave.co.nz"
API = "https://api-prod.paknsave.co.nz/v1/edge"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
ROOT = Path(__file__).resolve().parent.parent
STORES_FILE = ROOT / "config/paknsave.stores"


def call(url, body=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ponytail: fresh guest token per run (~200ms), valid ~1h. Long crawls call token() again on 401.
def token():
    return call(WEB + "/api/user/get-current-user",
                {"fingerprintUser": "cooklang-recipes", "fingerprintGuest": UA})["access_token"]


def my_stores():
    """[(id, label)] from config, or [( $PNS_STORE, 'env')]."""
    if os.environ.get("PNS_STORE"):
        return [(os.environ["PNS_STORE"], "env")]
    if not STORES_FILE.exists():
        sys.exit(f"No stores configured. Run: {sys.argv[0]} stores <town>  then add ids to {STORES_FILE}")
    out = []
    for ln in STORES_FILE.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            sid, _, label = ln.partition("  ")
            out.append((sid.strip(), label.strip() or sid.strip()))
    return out


def search(query, store, tok, limit=1, page=0, extra_filter=None):
    """One page of products. Algolia caps any single query at 1000 hits (20 pages of 50)."""
    filters = f"stores:{store}"
    if extra_filter:
        filters += f" AND {extra_filter}"
    q = {"attributesToHighlight": [], "attributesToRetrieve": ["productID"],
         "facets": [], "filters": filters, "highlightPostTag": "_", "highlightPreTag": "_",
         "hitsPerPage": limit, "maxValuesPerFacet": 1000, "page": page, "query": query,
         "analyticsTags": ["fs#WEB:desktop"]}
    body = {"algoliaQuery": q, "algoliaFacetQueries": [], "storeId": store,
            "hitsPerPage": limit, "page": page, "sortOrder": "NI_POPULARITY_ASC",
            "tobaccoQuery": True,
            "precisionMedia": {"adDomain": "SEARCH_PAGE" if query else "CATEGORY_PAGE",
                               "adPositions": [], "publishImpressionEvent": False, "disableAds": True}}
    return call(API + "/search/paginated/products", body, tok)


def facet_counts(store, tok, facet, extra_filter=None):
    """{value: count} for a facet — the only way to see past the 1000-hit cap."""
    filters = f"stores:{store}"
    if extra_filter:
        filters += f" AND {extra_filter}"
    q = {"attributesToHighlight": [], "attributesToRetrieve": ["productID"], "facets": [facet],
         "filters": filters, "highlightPostTag": "_", "highlightPreTag": "_", "hitsPerPage": 1,
         "maxValuesPerFacet": 1000, "page": 0, "query": "", "analyticsTags": ["fs#WEB:desktop"]}
    body = {"algoliaQuery": q, "algoliaFacetQueries": [], "storeId": store, "hitsPerPage": 1,
            "page": 0, "sortOrder": "NI_POPULARITY_ASC", "tobaccoQuery": True,
            "precisionMedia": {"adDomain": "CATEGORY_PAGE", "adPositions": [],
                               "publishImpressionEvent": False, "disableAds": True}}
    d = call(API + "/search/paginated/products", body, tok)
    return (d.get("algoliaSearchResult", {}).get("facets", {}) or {}).get(facet, {})


def all_stores(tok):
    return call(API + "/store", token=tok)["stores"]


def price_of(p):
    """(cents, unit_cents, measure) from a product. Prices are integer cents."""
    sp = p.get("singlePrice") or {}
    cmp_ = sp.get("comparativePrice") or {}
    return sp.get("price"), cmp_.get("pricePerUnit"), cmp_.get("measureDescription")


def label_of(p):
    return " ".join(x for x in (p.get("brand"), p.get("name"), p.get("displayName")) if x)


def line(term, p):
    if p is None:
        return f"{term:<24} {'—':>8}  not found", 0
    cents, unit, measure = price_of(p)
    per = f"  (${unit/100:.2f}/{measure})" if unit is not None else ""
    amount = f"${cents/100:.2f}" if cents is not None else "n/a"
    return f"{term:<24} {amount:>8}  {label_of(p)}{per}", (cents or 0)


def cmd_price(terms):
    if terms == ["-"]:
        terms = [t.strip() for t in sys.stdin if t.strip()]
    tok, store, total = token(), my_stores()[0][0], 0
    for t in terms:
        hits = search(t, store, tok, limit=1).get("products") or []
        row, cents = line(t, hits[0] if hits else None)
        print(row)
        total += cents
    print(f"{'TOTAL':<24} {'$%.2f' % (total / 100):>8}")


def cmd_stores(q):
    q = " ".join(q).lower()
    for s in all_stores(token()):
        if q in s["name"].lower() or q in s.get("address", "").lower():
            print(f"{s['id']}  {s['name']} — {s.get('address', '')}")


def demo():
    p = {"brand": "Pams", "name": "Pure Butter", "displayName": "500g",
         "singlePrice": {"price": 739, "comparativePrice": {"pricePerUnit": 148, "measureDescription": "100g"}}}
    row, cents = line("butter", p)
    assert "$7.39" in row and "$1.48/100g" in row and cents == 739, row
    assert price_of({}) == (None, None, None)
    assert "not found" in line("nope", None)[0]
    assert len(my_stores()) >= 1
    print("ok")


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    {"price": cmd_price, "stores": cmd_stores, "selftest": lambda _: demo()}.get(
        cmd, lambda _: sys.exit(__doc__))(args)

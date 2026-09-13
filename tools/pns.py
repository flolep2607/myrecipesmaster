#!/usr/bin/env python3
"""PAK'nSAVE price lookup. Guest token, no login, no browser.

  ./tools/pns.py stores taupo          # find your store id
  ./tools/pns.py price butter milk     # price named items
  cook shopping-list r.cook --ingredients-only | ./tools/pns.py price -

Store id comes from $PNS_STORE or config/paknsave.store.
"""
import json, os, sys, urllib.request
from pathlib import Path

WEB = "https://www.paknsave.co.nz"
API = "https://api-prod.paknsave.co.nz/v1/edge"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
ROOT = Path(__file__).resolve().parent.parent


def call(url, body=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": UA}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# ponytail: fresh guest token every run (~200ms). Cache to ~/.cache if you ever batch hundreds of lookups.
def token():
    return call(WEB + "/api/user/get-current-user",
                {"fingerprintUser": "cooklang-recipes", "fingerprintGuest": UA})["access_token"]


def store_id():
    s = os.environ.get("PNS_STORE") or (ROOT / "config/paknsave.store")
    if not isinstance(s, str):
        if not s.exists():
            sys.exit("No store set. Run: ./tools/pns.py stores <town>  then write the id to config/paknsave.store")
        s = s.read_text().strip()
    return s


def search(term, store, tok, limit=1):
    q = {
        "attributesToHighlight": [], "attributesToRetrieve": ["productID"],
        "facets": [], "filters": "stores:" + store,
        "highlightPostTag": "__/ais-highlight__", "highlightPreTag": "__ais-highlight__",
        "hitsPerPage": limit, "maxValuesPerFacet": 100, "page": 0, "query": term,
        "analyticsTags": ["fs#WEB:desktop"],
    }
    body = {"algoliaQuery": q, "algoliaFacetQueries": [], "storeId": store,
            "hitsPerPage": limit, "page": 0, "sortOrder": "NI_POPULARITY_ASC",
            "tobaccoQuery": True,
            "precisionMedia": {"adDomain": "SEARCH_PAGE", "adPositions": [],
                               "publishImpressionEvent": False, "disableAds": True}}
    return call(API + "/search/paginated/products", body, tok).get("products") or []


def line(term, p):
    """One priced row. Prices are integer cents."""
    if p is None:
        return f"{term:<24} —  not found"
    sp = p.get("singlePrice") or {}
    price = sp.get("price")
    cmp_ = sp.get("comparativePrice") or {}
    per = ""
    if cmp_.get("pricePerUnit") is not None:
        per = f"  (${cmp_['pricePerUnit']/100:.2f}/{cmp_.get('measureDescription', '')})"
    name = f"{p.get('brand', '')} {p.get('name', '')} {p.get('displayName', '')}".strip()
    amount = f"${price/100:.2f}" if price is not None else "price n/a"
    return f"{term:<24} {amount:>8}  {name}{per}", (price or 0)


def cmd_price(terms):
    if terms == ["-"]:
        terms = [t.strip() for t in sys.stdin if t.strip()]
    tok, store, total = token(), store_id(), 0
    for t in terms:
        hits = search(t, store, tok)
        out = line(t, hits[0] if hits else None)
        if isinstance(out, tuple):
            print(out[0])
            total += out[1]
        else:
            print(out)
    print(f"{'TOTAL':<24} {'$%.2f' % (total/100):>8}")


def cmd_stores(q):
    q = " ".join(q).lower()
    for s in call(API + "/store", token=token())["stores"]:
        if q in s["name"].lower() or q in s.get("address", "").lower():
            print(f"{s['id']}  {s['name']} — {s.get('address', '')}")


def demo():
    p = {"brand": "Pams", "name": "Pure Butter", "displayName": "500g",
         "singlePrice": {"price": 739, "comparativePrice": {"pricePerUnit": 148, "measureDescription": "100g"}}}
    row, cents = line("butter", p)
    assert "$7.39" in row and "$1.48/100g" in row and cents == 739, row
    assert "not found" in line("nope", None)
    print("ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    {"price": cmd_price, "stores": cmd_stores, "selftest": lambda _: demo()}.get(
        cmd, lambda _: sys.exit(__doc__))(args)

#!/usr/bin/env python3
"""PAK'nSAVE catalogue + price history, one SQLite file, run weekly.

  ./tools/pns_db.py sync                 # crawl every store in config/paknsave.stores
  ./tools/pns_db.py compare butter       # today's price at each store, cheapest first
  ./tools/pns_db.py history "pams butter"  # price over time
  ./tools/pns_db.py stats

Weekly:  0 7 * * 1  cd ~/recipes && ./tools/pns_db.py sync >> data/sync.log 2>&1
"""
import json, sqlite3, sys, time, urllib.error
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pns

DB = pns.ROOT / "data/paknsave.db"
PAGE = 50            # Algolia max hits per page
MAX_PAGES = 20       # Algolia caps any one query at 1000 hits
PAUSE = 0.12         # be polite

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS products(
  product_id TEXT PRIMARY KEY, brand TEXT, name TEXT, size TEXT, sale_type TEXT,
  category0 TEXT, category1 TEXT, category2 TEXT, tags TEXT, seen TEXT,
  ean TEXT, ingredients TEXT, detailed TEXT);
CREATE TABLE IF NOT EXISTS nutrition(
  product_id TEXT PRIMARY KEY, basis TEXT, kcal REAL, protein REAL, carbs REAL,
  sugars REAL, fat REAL, sat_fat REAL, fibre REAL, sodium_mg REAL);
CREATE TABLE IF NOT EXISTS prices(
  product_id TEXT, store_id TEXT, day TEXT, cents INTEGER,
  unit_cents INTEGER, unit_measure TEXT, raw TEXT,
  PRIMARY KEY(product_id, store_id, day));
CREATE TABLE IF NOT EXISTS runs(store_id TEXT, day TEXT, products INTEGER, seconds REAL,
  PRIMARY KEY(store_id, day));
CREATE INDEX IF NOT EXISTS prices_by_product ON prices(product_id, day);
CREATE INDEX IF NOT EXISTS products_by_name ON products(name);
"""


def db():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(products)")}
    for col in ("ean", "ingredients", "detailed"):   # for DBs created before enrich existed
        if col not in have:
            c.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
    c.commit()
    return c


class Api:
    """pns.search with one token refresh on 401 — a full crawl outlives a guest token."""

    def __init__(self):
        self.tok = pns.token()

    def _retry(self, fn):
        try:
            return fn(self.tok)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.tok = pns.token()
            return fn(self.tok)

    def search(self, store, page, filt):
        return self._retry(lambda t: pns.search("", store, t, limit=PAGE, page=page, extra_filter=filt))

    def product(self, store, pid):
        return self._retry(lambda t: pns.call(f"{pns.API}/store/{store}/product/{pid}", token=t))

    def facets(self, store, facet):
        return self._retry(lambda t: pns.facet_counts(store, t, facet))


def tree(p):
    """First non-'Featured' category tree — Featured/promo trees are duplicates of the real one."""
    trees = p.get("categoryTrees") or [{}]
    real = [t for t in trees if t.get("level0") != "Featured"] or trees
    t = real[0]
    return t.get("level0"), t.get("level1"), t.get("level2")


def rows_for(p, store, day):
    cents, unit, measure = pns.price_of(p)
    c0, c1, c2 = tree(p)
    tags = ",".join(f.get("itemDescription", "") for f in (p.get("facets") or []))
    product = (p["productId"], p.get("brand"), p.get("name"), p.get("displayName"),
               p.get("saleType"), c0, c1, c2, tags, day)
    price = (p["productId"], store, day, cents, unit, measure,
             json.dumps(p.get("singlePrice") or {}, separators=(",", ":")))
    return product, price


def sync_store(conn, api, store, label, day):
    t0, seen = time.time(), {}
    buckets = api.facets(store, "category1NI")
    print(f"{label}: {len(buckets)} categories, {sum(buckets.values())} slots", flush=True)
    for i, (cat, count) in enumerate(sorted(buckets.items()), 1):
        pages = min(-(-count // PAGE), MAX_PAGES)
        for page in range(pages):
            esc = cat.replace('"', '\\"')
            got = api.search(store, page, f'category1NI:"{esc}"').get("products") or []
            for p in got:
                seen[p["productId"]] = p
            time.sleep(PAUSE)
            if len(got) < PAGE:
                break
        print(f"\r  {i}/{len(buckets)} {len(seen)} products", end="", flush=True)
    # ponytail: category crawl misses anything with no category1NI. This catches the popular
    # ones (the 1000-hit window); a product both uncategorised AND unpopular stays invisible.
    stray = []
    for pg in range(MAX_PAGES):
        got = api.search(store, pg, None).get("products") or []
        if not got:
            break
        stray += [p for p in got if p["productId"] not in seen]
        time.sleep(PAUSE)
    for p in stray:
        seen[p["productId"]] = p
    if stray:
        print(f"\n  + {len(stray)} uncategorised", end="")

    prod, price = zip(*(rows_for(p, store, day) for p in seen.values())) if seen else ([], [])
    conn.executemany(
        "INSERT INTO products(product_id,brand,name,size,sale_type,category0,category1,category2,tags,seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET "
        "brand=excluded.brand, name=excluded.name, size=excluded.size, sale_type=excluded.sale_type, "
        "category0=excluded.category0, category1=excluded.category1, category2=excluded.category2, "
        "tags=excluded.tags, seen=excluded.seen", prod)
    conn.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)", price)
    conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                 (store, day, len(seen), round(time.time() - t0, 1)))
    conn.commit()
    print(f"\r  {label}: {len(seen)} products in {time.time() - t0:.0f}s" + " " * 20, flush=True)


# PAK'nSAVE nutrient codes -> our columns. Energy arrives in kJ, sodium in mg.
NUTRIENTS = {"ENER-": "kcal", "PRO-": "protein", "CHO-": "carbs", "SUGAR": "sugars",
             "FAT": "fat", "FASAT": "sat_fat", "FIBTG": "fibre", "NA": "sodium_mg"}


def nutrition_of(detail):
    """Per-100g/ml nutrients. {} when the product carries none (fresh produce, most often)."""
    out = {}
    for n in (detail.get("nutritionalInfo") or {}).get("nutrients") or []:
        # BY_SERVING rows repeat these at an arbitrary serving size; only per-100 is comparable.
        if n.get("nutrientBasisQuantityType") != "BY_MEASURE" or n.get("nutrientBasisQty") != 100:
            continue
        key = NUTRIENTS.get(n.get("nutrientType"))
        if not key:
            continue
        qty = n.get("qtyContained")
        out[key] = round(qty / 4.184, 1) if key == "kcal" and qty is not None else qty
        out["basis"] = "ml" if n.get("nutrientBasisQtyUom") == "MLT" else "g"
    return out


def cmd_enrich(args):
    """Fill ean/ingredients/nutrition from the per-product endpoint. Only touches products
    never detailed before, so the weekly run costs one request per genuinely new product."""
    conn, api = db(), Api()
    store = pns.my_stores()[0][0]
    limit = int(args[0]) if args else 0
    todo = [r[0] for r in conn.execute(
        "SELECT product_id FROM products WHERE detailed IS NULL ORDER BY product_id")]
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} products to detail", flush=True)
    day, done, with_nut = date.today().isoformat(), 0, 0
    for pid in todo:
        try:
            d = api.product(store, pid)
        except urllib.error.HTTPError as e:
            conn.execute("UPDATE products SET detailed = ? WHERE product_id = ?", (f"{day} http{e.code}", pid))
            continue
        nut = nutrition_of(d)
        conn.execute("UPDATE products SET ean = ?, ingredients = ?, detailed = ? WHERE product_id = ?",
                     (d.get("sku"), d.get("ingredientStatement"), day, pid))
        if nut:
            with_nut += 1
            conn.execute(
                "INSERT OR REPLACE INTO nutrition(product_id,basis,kcal,protein,carbs,sugars,fat,sat_fat,fibre,sodium_mg)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, nut.get("basis"), nut.get("kcal"), nut.get("protein"), nut.get("carbs"),
                 nut.get("sugars"), nut.get("fat"), nut.get("sat_fat"), nut.get("fibre"), nut.get("sodium_mg")))
        done += 1
        if done % 25 == 0:
            conn.commit()
            print(f"\r  {done}/{len(todo)}  {with_nut} with nutrition", end="", flush=True)
        time.sleep(PAUSE)
    conn.commit()
    print(f"\r  detailed {done}, {with_nut} with nutrition" + " " * 20)


def cmd_sync(_):
    conn, api, day = db(), Api(), date.today().isoformat()
    for sid, label in pns.my_stores():
        conn.execute("INSERT OR REPLACE INTO stores VALUES (?,?)", (sid, label))
        sync_store(conn, api, sid, label, day)
    cmd_stats([])


def _match(conn, term):
    like = f"%{term.lower()}%"
    return conn.execute(
        "SELECT product_id, brand, name, size FROM products "
        "WHERE lower(brand || ' ' || name || ' ' || ifnull(size,'')) LIKE ? "
        "ORDER BY length(name) LIMIT 12", (like,)).fetchall()


def cmd_compare(args):
    conn, term = db(), " ".join(args)
    hits = _match(conn, term)
    if not hits:
        sys.exit(f"nothing matching {term!r} in the DB — run sync first")
    labels = dict(conn.execute("SELECT id, label FROM stores"))
    for pid, brand, name, size in hits[:5]:
        print(f"\n{brand or ''} {name} {size or ''}".rstrip())
        rows = conn.execute(
            "SELECT store_id, cents, unit_cents, unit_measure, day FROM prices WHERE product_id = ? "
            "AND day = (SELECT max(day) FROM prices WHERE product_id = ?)", (pid, pid)).fetchall()
        for sid, cents, unit, measure, day in sorted(rows, key=lambda r: r[1] or 1 << 30):
            per = f"  ${unit/100:.2f}/{measure}" if unit else ""
            print(f"  {labels.get(sid, sid):<12} ${cents/100:>7.2f}{per}   ({day})")


def cmd_history(args):
    conn, term = db(), " ".join(args)
    hits = _match(conn, term)
    if not hits:
        sys.exit(f"nothing matching {term!r} in the DB")
    pid, brand, name, size = hits[0]
    labels = dict(conn.execute("SELECT id, label FROM stores"))
    print(f"{brand or ''} {name} {size or ''}".rstrip())
    for day, sid, cents in conn.execute(
            "SELECT day, store_id, cents FROM prices WHERE product_id = ? ORDER BY day, store_id", (pid,)):
        print(f"  {day}  {labels.get(sid, sid):<12} ${cents/100:.2f}")


def cmd_stats(_):
    conn = db()
    n_p = conn.execute("SELECT count(*) FROM products").fetchone()[0]
    n_pr = conn.execute("SELECT count(*) FROM prices").fetchone()[0]
    days = conn.execute("SELECT count(DISTINCT day) FROM prices").fetchone()[0]
    print(f"\n{n_p} products, {n_pr} price rows over {days} day(s), {DB}")
    for sid, day, n, secs in conn.execute("SELECT * FROM runs ORDER BY day DESC, store_id LIMIT 9"):
        label = dict(conn.execute("SELECT id, label FROM stores")).get(sid, sid)
        print(f"  {day}  {label:<12} {n:>6} products  {secs:>6.0f}s")


def demo():
    p = {"productId": "X-EA-000", "brand": "Pams", "name": "Pure Butter", "displayName": "500g",
         "saleType": "UNITS", "facets": [{"itemDescription": "Halal"}],
         "categoryTrees": [{"level0": "Featured", "level1": "Pams", "level2": "Deli"},
                           {"level0": "Fridge, Deli & Eggs", "level1": "Butter & Margarine", "level2": "Butter"}],
         "singlePrice": {"price": 739, "comparativePrice": {"pricePerUnit": 148, "measureDescription": "100g"}}}
    assert tree(p) == ("Fridge, Deli & Eggs", "Butter & Margarine", "Butter"), tree(p)
    prod, price = rows_for(p, "S1", "2026-09-13")
    assert prod[:4] == ("X-EA-000", "Pams", "Pure Butter", "500g") and prod[8] == "Halal"
    assert price[:6] == ("X-EA-000", "S1", "2026-09-13", 739, 148, "100g")
    # a product with only a Featured tree still gets one
    assert tree({"categoryTrees": [{"level0": "Featured", "level1": "X", "level2": "Y"}]}) == ("Featured", "X", "Y")
    coke = {"nutritionalInfo": {"nutrients": [
        {"nutrientType": "ENER-", "nutrientBasisQuantityType": "BY_MEASURE", "nutrientBasisQty": 100,
         "nutrientBasisQtyUom": "MLT", "qtyContained": 1.2},
        {"nutrientType": "ENER-", "nutrientBasisQuantityType": "BY_SERVING", "nutrientBasisQty": 250,
         "nutrientBasisQtyUom": "MLT", "qtyContained": 3},
        {"nutrientType": "NA", "nutrientBasisQuantityType": "BY_MEASURE", "nutrientBasisQty": 100,
         "nutrientBasisQtyUom": "MLT", "qtyContained": 4.1}]}}
    n = nutrition_of(coke)
    assert n["kcal"] == 0.3 and n["sodium_mg"] == 4.1 and n["basis"] == "ml", n
    assert "kj" not in n and 3 not in n.values(), "BY_SERVING row leaked in"
    assert nutrition_of({}) == {}, "fresh produce has no nutrition"
    print("ok")


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    {"sync": cmd_sync, "enrich": cmd_enrich, "compare": cmd_compare, "history": cmd_history,
     "stats": cmd_stats, "selftest": lambda _: demo()}.get(cmd, lambda _: sys.exit(__doc__))(args)

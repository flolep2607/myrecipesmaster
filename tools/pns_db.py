#!/usr/bin/env python3
"""PAK'nSAVE catalogue + price history, one SQLite file, run weekly.

  ./tools/pns_db.py sync                 # crawl every store in config/paknsave.stores
  ./tools/pns_db.py compare butter       # today's price at each store, cheapest first
  ./tools/pns_db.py history "pams butter"  # price over time
  ./tools/pns_db.py ingredient flour     # pin a product to an ingredient, write its yaml
  ./tools/pns_db.py prices               # refresh every pinned price after a sync
  ./tools/pns_db.py basket Pancakes.cook:2 ...   # what the list costs at each store
  ./tools/pns_db.py table                # the whole mapping as markdown
  ./tools/pns_db.py stats

Weekly:  0 7 * * 1  cd ~/recipes && ./tools/pns_db.py sync >> data/sync.log 2>&1
"""
import json, re, sqlite3, subprocess, sys, time, urllib.error
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pns

DB = pns.ROOT / "data/paknsave.db"
PAGE = 50            # Algolia max hits per page
MAX_PAGES = 20       # Algolia caps any one query at 1000 hits
PAUSE = 0.12         # be polite

MAP_FILE = pns.ROOT / "config/products.map"      # ingredient -> product_id, hand-editable
WEIGHTS_FILE = pns.ROOT / "config/unit_weights.conf"   # grams per countable unit, hand-editable
NUT_DIR = pns.ROOT / "datastore/ingredients"     # nutrition, committed
PRICE_DIR = pns.ROOT / "data/prices/ingredients" # today's prices, gitignored, regenerated
# PAK'nSAVE's comparative price, as (amount of base units, base)
LABEL = "lower(ifnull(p.brand,'') || ' ' || p.name || ' ' || ifnull(p.size,''))"
CATS = "lower(ifnull(p.category1,'') || ' ' || ifnull(p.category2,''))"
NON_FOOD = {"household & cleaning", "health & body", "pets", "baby & toddler"}
PRESERVED = {"canned", "tinned", "frozen", "dried", "instant", "pickled"}
BASES = {"100g": (100, "g"), "1kg": (1000, "g"), "100ml": (100, "ml"), "1l": (1000, "ml"),
         "ea": (1, "each"), "1ea": (1, "each")}
# recipe units we can turn into those bases; anything else (tbsp, pinch, large) is countable
UNITS = {"g": (1, "g"), "kg": (1000, "g"), "ml": (1, "ml"), "l": (1000, "ml")}

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
  unit_cents INTEGER, unit_measure TEXT, promo INTEGER, promo_type TEXT, raw TEXT,
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
    for table, cols in (("products", {"ean": "TEXT", "ingredients": "TEXT", "detailed": "TEXT"}),
                        ("prices", {"promo": "INTEGER", "promo_type": "TEXT"})):
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():          # migrate DBs made before these columns existed
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
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
    sp, promos = p.get("singlePrice") or {}, p.get("promotions") or []
    # On special, the product carries promotions[] and singlePrice.promoId, and `cents` is
    # already the reduced price — `promo` says whether that number is a special or the shelf price.
    promo = 1 if (promos or sp.get("promoId")) else 0
    promo_type = promos[0].get("rewardType") if promos else None
    price = (p["productId"], store, day, cents, unit, measure, promo, promo_type,
             json.dumps({"singlePrice": sp, "promotions": promos}, separators=(",", ":")))
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
    conn.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?)", price)
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
        "WHERE lower(ifnull(brand,'') || ' ' || name || ' ' || ifnull(size,'')) LIKE ? "
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
            "SELECT store_id, cents, unit_cents, unit_measure, promo, promo_type, day FROM prices "
            "WHERE product_id = ? AND day = (SELECT max(day) FROM prices WHERE product_id = ?)",
            (pid, pid)).fetchall()
        for sid, cents, unit, measure, promo, ptype, day in sorted(rows, key=lambda r: r[1] or 1 << 30):
            per = f"  ${unit/100:.2f}/{measure}" if unit else ""
            tag = f"  SPECIAL ({ptype})" if promo else ""
            print(f"  {labels.get(sid, sid):<12} ${cents/100:>7.2f}{per}{tag}   ({day})")


def cmd_history(args):
    conn, term = db(), " ".join(args)
    hits = _match(conn, term)
    if not hits:
        sys.exit(f"nothing matching {term!r} in the DB")
    pid, brand, name, size = hits[0]
    labels = dict(conn.execute("SELECT id, label FROM stores"))
    print(f"{brand or ''} {name} {size or ''}".rstrip())
    for day, sid, cents, promo in conn.execute(
            "SELECT day, store_id, cents, promo FROM prices WHERE product_id = ? "
            "ORDER BY day, store_id", (pid,)):
        print(f"  {day}  {labels.get(sid, sid):<12} ${cents/100:>7.2f}{'  SPECIAL' if promo else ''}")


def cmd_stats(_):
    conn = db()
    n_p = conn.execute("SELECT count(*) FROM products").fetchone()[0]
    n_pr = conn.execute("SELECT count(*) FROM prices").fetchone()[0]
    days = conn.execute("SELECT count(DISTINCT day) FROM prices").fetchone()[0]
    print(f"\n{n_p} products, {n_pr} price rows over {days} day(s), {DB}")
    for sid, day, n, secs in conn.execute("SELECT * FROM runs ORDER BY day DESC, store_id LIMIT 9"):
        label = dict(conn.execute("SELECT id, label FROM stores")).get(sid, sid)
        print(f"  {day}  {label:<12} {n:>6} products  {secs:>6.0f}s")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


SIZE = re.compile(r"([\d.]+)\s*(kg|g|ml|l|ea|pk)\b", re.I)


def from_size(cents, size):
    """Fallback for the ~12% of rows with no comparative price: divide the shelf price by the
    pack size on the label. "Pams Iodised Table Salt 1kg" -> cents per gram."""
    m = SIZE.search(size or "")
    if not m or not cents:
        return None, None
    amount, unit = float(m[1]), m[2].lower()
    mult, base = {"g": (1, "g"), "kg": (1000, "g"), "ml": (1, "ml"), "l": (1000, "ml"),
                  "ea": (1, "each"), "pk": (1, "each")}[unit]
    return (cents / (amount * mult), base) if amount else (None, None)


def per_base(unit_cents, measure):
    """Cents per gram/ml/item, from the shelf's comparative price. (None, None) if it has none."""
    m = BASES.get((measure or "").lower())
    return (unit_cents / m[0], m[1]) if m and unit_cents else (None, None)


def to_base(value, unit, grams=None):
    """A recipe quantity as (amount, base). Countable things have no unit, or one we can't weigh
    (large, clove): they price per item, or by weight when config/unit_weights.conf knows what
    one of them weighs."""
    mult, base = UNITS.get((unit or "").lower(), (None, None))
    if base:
        return value * mult, base
    return (value * grams, "g") if grams else (value, "each")


def weights():
    """{ingredient: grams for one of them} — one clove, one onion, one chicken breast."""
    out = {}
    for ln in (WEIGHTS_FILE.read_text().splitlines() if WEIGHTS_FILE.exists() else []):
        ln = ln.split("#")[0].strip()
        if ln:
            name, _, g = ln.rpartition(" ")
            out[name.strip()] = float(g)
    return out


def map_key(line):
    """The ingredient name in a products.map line: '<name> <product_id>  # label'."""
    return line.split("#")[0].strip().rpartition(" ")[0]


def pins():
    """{ingredient: product_id} from config/products.map."""
    out = {}
    for ln in (MAP_FILE.read_text().splitlines() if MAP_FILE.exists() else []):
        if map_key(ln):
            out[map_key(ln)] = ln.split("#")[0].strip().rpartition(" ")[2]
    return out


def pin(name, pid, label):
    lines = [ln for ln in (MAP_FILE.read_text().splitlines() if MAP_FILE.exists() else [])
             if map_key(ln) != name]
    lines.append(f"{name} {pid}  # {label}")
    MAP_FILE.write_text("\n".join(sorted(l for l in lines if l.strip())) + "\n")


def singular(w):
    """Crude but careful: Sauces -> sauce, not sauc, or "soy sauce" stops matching Soy Sauces."""
    if w.endswith(("oes", "ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def words(s):
    """Lowercase words, singularised, so Eggs/Tomatoes/Shallots match egg/tomato/shallot."""
    return {singular(w) for w in re.findall(r"[a-z]+", (s or "").lower())}


def cat_score(term_words, c1, c2):
    """How well a shelf category fits an ingredient: how much of the ingredient it covers, then
    how much else it drags in. 'Butter' beats 'Peanut & Nut Butter'; 'Olive & Avocado Oil' beats
    'Dairy Free Spreads'; 'Onions, Leeks & Shallots' beats 'Other Frozen Vegetables'."""
    def level(name):
        cat = words(name)
        hit = len(term_words & cat) / len(term_words)
        # a category that matches nothing must not win on being short ("Sparkling Water" for lemon)
        return (-hit, len(cat - term_words) if hit else 0)

    # score the levels apart and keep the best: "Chilli, Garlic & Ginger" is a tight fit for
    # garlic, but reading it together with its parent buries it under garlic bread
    return min(level(c1 or ""), level(c2 or ""))


def candidates(conn, term, store, limit=10):
    """Products matching `term` with a price today. A bare LIKE ranks body wash above milk and
    Leggo's above eggs, so rank on the shelf's own category tree first, then the product name."""
    sql = ("SELECT p.product_id, trim(ifnull(p.brand,'') || ' ' || p.name || ' ' || ifnull(p.size,'')),"
           "       p.name, p.category0, p.category1, p.category2, pr.cents, pr.unit_cents, pr.unit_measure "
           "FROM products p JOIN prices pr ON pr.product_id = p.product_id "
           "WHERE pr.store_id = ? AND pr.day = (SELECT max(day) FROM prices) AND ")
    # every word of the ingredient somewhere in the label, or any one word in the category tree:
    # no product is called "canned tomato", the only "vegetable oil" on the shelf is a sardine,
    # and the cooking oil aisle is called "Oil & Vinegar"
    parts = [singular(w) for w in term.lower().split()]
    where = ("(" + " AND ".join([f"{LABEL} LIKE ?"] * len(parts)) + " OR "
             + " OR ".join([f"{CATS} LIKE ?"] * len(parts)) + ")")
    rows = conn.execute(sql + where, (store, *(f"%{w}%" for w in parts * 2))).fetchall()
    tw = words(term)
    fresh = not (tw & PRESERVED)
    # English compounds put the head noun last: garlic POWDER, vegetable OIL, reduced sugar
    # KETCHUP. Without this, "garlic powder" lands on fresh garlic and "vegetable oil" on carrots.
    head = parts[-1]

    def rank(r):
        _, _, name, c0, c1, c2, cents, unit_cents, measure = r
        recall, extra = cat_score(tw, c1, c2)
        per, _ = per_base(unit_cents, measure)
        return (head not in words(name) | words(f"{c1 or ''} {c2 or ''}"),
                # a product literally called Onion Powder beats the best-matching shelf category,
                # because plenty of ingredients have no category of their own
                not tw <= words(name),
                (c0 or "").lower() in NON_FOOD,
                # @tomato means the fresh one; canned tomatoes are cheaper per 100g and would
                # win every tie. A recipe that wants them says @canned tomato.
                not (fresh and c0 == "Fruit & Vegetables"),
                recall,
                extra,
                per if per else (cents or 1 << 30))

    scored = sorted(rows, key=rank)
    if not scored:
        return []
    # cents/each is not comparable with cents/gram, so among the rows that matched equally well,
    # price against whichever of the two the tier mostly uses: eggs by the dozen, not egg white
    # by the litre
    tier = rank(scored[0])[:6]
    group = lambda r: per_base(r[7], r[8])[1] == "each"   # a gram and a millilitre are the same
    groups = [group(r) for r in scored if rank(r)[:6] == tier]   # money; an item is not
    modal = max(set(groups), key=groups.count)
    scored.sort(key=lambda r: rank(r)[:6] + (group(r) != modal,) + rank(r)[6:])
    return [(r[0], r[1], r[6], r[7], r[8]) for r in scored[:limit]]




def write_price(conn, name, pid, store, label):
    """data/prices/ingredients/<name>.yaml — what cost.j2 reads. Never goes in datastore/."""
    row = conn.execute(
        "SELECT cents, unit_cents, unit_measure, promo, day FROM prices "
        "WHERE product_id = ? AND store_id = ? ORDER BY day DESC LIMIT 1", (pid, store)).fetchone()
    if not row:
        return None
    cents, unit_cents, measure, promo, day = row
    per, base = per_base(unit_cents, measure)
    if not per:
        size = conn.execute("SELECT size FROM products WHERE product_id = ?", (pid,)).fetchone()
        per, base = from_size(cents, size[0] if size else None)
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    gpu = weights().get(name)
    (PRICE_DIR / f"{slug(name)}.yaml").write_text(
        f"# {label} — regenerate with ./tools/pns_db.py prices\n"
        f"product_id: {pid}\ncents: {cents}\n"
        + (f"cents_per_base: {per:.4f}\nbase: {base}\n" if per else "")
        + (f"grams_per_unit: {gpu:g}\n" if gpu else "")
        + f"promo: {1 if promo else 0}\nday: {day}\n")
    return per, base


def write_nutrition(conn, name, pid, label):
    """datastore/ingredients/<name>.yaml, straight from the enrich data. None when the
    product carries no nutrition — fresh produce mostly, and anything enrich hasn't reached."""
    row = conn.execute("SELECT basis, kcal, protein, carbs, fat FROM nutrition WHERE product_id = ?",
                       (pid,)).fetchone()
    if not row or row[1] is None:
        return None
    basis, kcal, protein, carbs, fat = row
    NUT_DIR.mkdir(parents=True, exist_ok=True)
    (NUT_DIR / f"{slug(name)}.yaml").write_text(
        f"# per 100 {basis} — {label}\nkcal: {kcal}\nprotein: {protein}\n"
        f"carbs: {carbs}\nfat: {fat}\n")
    return kcal


def cmd_ingredient(args):
    """Pin a product to an ingredient name and write its yaml files."""
    which, search = 0, None
    for flag in ("-p", "-s"):                  # -p picks another match, -s searches other words
        if flag in args:
            i = args.index(flag)
            if flag == "-p":
                which = int(args[i + 1]) - 1
            else:
                search = args[i + 1].lower()
            args = args[:i] + args[i + 2:]
    conn, name, store = db(), " ".join(args).lower(), pns.my_stores()[0]
    if not name:
        sys.exit('usage: pns_db.py ingredient <name> [-p N] [-s "other search words"]')
    hits = candidates(conn, search or name, store[0])
    if not hits:
        sys.exit(f"nothing matching {search or name!r} priced at {store[1]} — run sync first")
    pid, label, cents, unit_cents, measure = hits[which]
    pin(name, pid, label)
    priced = write_price(conn, name, pid, store[0], label)
    kcal = write_nutrition(conn, name, pid, label)
    per = ("no unit price" if not (priced and priced[0])
           else f"${priced[0] / 100:.2f} each" if priced[1] == "each"
           else f"${priced[0]:.2f}/100{priced[1]}")
    print(f"{name} -> {label}  ${cents / 100:.2f}  {per}  @ {store[1]}")
    print(f"  nutrition: {kcal} kcal/100" + (" — run enrich" if kcal is None else ""))
    for n, (_, lab, c, uc, m) in enumerate(hits, 1):
        if n - 1 != which:
            print(f"  -p {n}  {lab}  ${c / 100:.2f}" + (f"  ${uc / 100:.2f}/{m}" if uc else ""))


def cmd_table(_):
    """The mapping as markdown: what each ingredient buys, what it costs, what it's made of."""
    conn, grams = db(), weights()
    store = pns.my_stores()[0]
    print(f"| ingredient | product | price | per | one is | kcal/100 |\n|---|---|---|---|---|---|")
    for name, pid in sorted(pins().items()):
        row = conn.execute(
            "SELECT trim(ifnull(p.brand,'') || ' ' || p.name || ' ' || ifnull(p.size,'')), "
            "       pr.cents, pr.unit_cents, pr.unit_measure, p.size, n.kcal "
            "FROM products p LEFT JOIN prices pr ON pr.product_id = p.product_id "
            "  AND pr.store_id = ? AND pr.day = (SELECT max(day) FROM prices) "
            "LEFT JOIN nutrition n ON n.product_id = p.product_id WHERE p.product_id = ?",
            (store[0], pid)).fetchone()
        if not row:
            continue
        label, cents, unit_cents, measure, size, kcal = row
        per, base = per_base(unit_cents, measure)
        if not per:
            per, base = from_size(cents, size)
        unit = (f"${per / 100:.2f} each" if base == "each" else f"${per:.2f}/100{base}") if per else ""
        g = f"{grams[name]:g} g" if name in grams else ""
        print(f"| {name} | {label} | ${cents / 100:.2f} | {unit} | {g} | {kcal or ''} |")


def cmd_suggest(args):
    """Print the candidate products for a name without pinning anything."""
    conn, store = db(), pns.my_stores()[0]
    term = " ".join(a for a in args if a != "-s").lower()
    for n, (pid, label, cents, unit_cents, measure) in enumerate(candidates(conn, term, store[0]), 1):
        per, base = per_base(unit_cents, measure)
        if not per:
            per, base = from_size(cents, conn.execute(
                "SELECT size FROM products WHERE product_id = ?", (pid,)).fetchone()[0])
        unit = (f"${per / 100:.2f} each" if base == "each" else f"${per:.2f}/100{base}") if per else "?"
        cat = conn.execute("SELECT category0 || ' > ' || ifnull(category1,'') || ' > ' || "
                           "ifnull(category2,'') FROM products WHERE product_id = ?", (pid,)).fetchone()[0]
        print(f"{n}. {label}  ${cents / 100:.2f}  {unit}  [{cat}]")


def cmd_apply(args):
    """Read decisions on stdin and pin them all: `<ingredient> | <search or -> | <n> | <grams or ->`,
    the format the ingredient-picking agents report in."""
    conn, store = db(), pns.my_stores()[0]
    lines = sys.stdin if args == ["-"] else Path(args[0]).read_text().splitlines()
    gram_lines, done = [], 0
    for ln in lines:
        parts = [f.strip() for f in ln.split("|")]
        if len(parts) != 4 or not parts[0] or parts[0].startswith("#"):
            continue
        name, search, pick, grams = parts
        hits = candidates(conn, (search if search != "-" else name).lower(), store[0])
        n = int(pick) - 1 if pick.isdigit() else 0
        if not hits or n >= len(hits):
            print(f"  no match for {name} ({search})", file=sys.stderr)
            continue
        pid, label = hits[n][0], hits[n][1]
        pin(name.lower(), pid, label)
        if grams not in ("-", ""):
            gram_lines.append(f"{name.lower()} {grams}")
        done += 1
        print(f"{name} -> {label}")
    if gram_lines:
        keep = [ln for ln in (WEIGHTS_FILE.read_text().splitlines() if WEIGHTS_FILE.exists() else [])
                if ln.split("#")[0].strip().rpartition(" ")[0] not in
                {g.rpartition(" ")[0] for g in gram_lines}]
        WEIGHTS_FILE.write_text("\n".join(sorted(x for x in keep + gram_lines if x.strip())) + "\n")
    print(f"{done} pinned, {len(gram_lines)} unit weights", file=sys.stderr)
    cmd_prices([])


def cmd_prices(_):
    """Rewrite every pinned price yaml from the newest sync."""
    conn, store = db(), pns.my_stores()[0]
    n = 0
    for name, pid in pins().items():
        label = conn.execute("SELECT trim(ifnull(brand,'') || ' ' || name) FROM products "
                             "WHERE product_id = ?", (pid,)).fetchone()
        n += bool(write_price(conn, name, pid, store[0], label[0] if label else pid))
    print(f"{n} price files in {PRICE_DIR} @ {store[1]}")


def qty_value(v):
    """Plain numbers arrive as regular; 1/2 tbsp arrives as whole+num/den; text has none."""
    inner = ((v or {}).get("value") or {}).get("value")
    if isinstance(inner, dict):
        return inner.get("whole", 0) + inner["num"] / inner["den"] if inner.get("den") else None
    return inner if isinstance(inner, (int, float)) else None


def shopping_list(args):
    """[(name, value, unit)] from `cook shopping-list -f json`."""
    out = subprocess.run(["cook", "shopping-list", "-f", "json", *args],
                         capture_output=True, text=True, cwd=pns.ROOT)
    if out.returncode:
        sys.exit(out.stderr.strip() or "cook shopping-list failed")
    items = []
    for cat in json.loads(out.stdout):
        for i in cat["items"]:
            q = i["quantity"][0] if i["quantity"] else {}
            items.append((i["name"], qty_value(q.get("value")), q.get("unit")))
    return items


def same(a, b):
    """A gram of soy sauce is a millilitre of it, close enough to price a recipe with."""
    return a == b or {a, b} == {"g", "ml"}


def cmd_basket(args):
    """What one shopping list costs at each store, valued at today's unit prices."""
    if not args:
        sys.exit("usage: pns_db.py basket <recipe>[:scale] ...")
    conn, mapped, grams = db(), pins(), weights()
    stores = list(conn.execute("SELECT id, label FROM stores"))
    day = conn.execute("SELECT max(day) FROM prices").fetchone()[0]
    totals = {label: 0.0 for _, label in stores}
    skipped, partial, nounit = [], [], []
    for name, value, unit in shopping_list(args):
        pid = mapped.get(name.lower())
        if not pid or value is None:
            skipped.append(name if pid else f"{name} (not pinned)")
            continue
        amount, base = to_base(float(value), unit, grams.get(name.lower()))
        size = conn.execute("SELECT size FROM products WHERE product_id = ?", (pid,)).fetchone()
        costs = {}
        for sid, label in stores:
            row = conn.execute("SELECT cents, unit_cents, unit_measure FROM prices "
                               "WHERE product_id = ? AND store_id = ? AND day = ?",
                               (pid, sid, day)).fetchone()
            if not row:
                continue
            per, pbase = per_base(row[1], row[2])
            if not (per and same(pbase, base)):
                per, pbase = from_size(row[0], size and size[0])
            if per and same(pbase, base):
                costs[label] = per * amount
        # an item missing at one store would make that store look cheaper, so drop it from all
        if len(costs) == len(stores):
            totals.update({k: totals[k] + v for k, v in costs.items()})
        elif costs:
            partial.append(name)
        else:
            nounit.append(f"{name} ({unit or 'no unit'})")
    if not any(totals.values()):
        sys.exit("nothing priced — pin ingredients with: pns_db.py ingredient <name>")
    best = min(totals.values())
    print(f"\nbasket at {day}, valued at unit prices (not pack sizes)")
    for label, cents in sorted(totals.items(), key=lambda kv: kv[1]):
        print(f"  {label:<12} ${cents / 100:>7.2f}" +
              (f"  +${(cents - best) / 100:.2f}" if cents > best else "  cheapest"))
    for title, items in (("not pinned or no quantity", skipped),
                         ("no weight or volume to price", nounit),
                         ("not stocked at every store, left out of every total", partial)):
        if items:
            print(f"\n{title}: " + ", ".join(sorted(set(items))))


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
    assert price[6] == 0 and price[7] is None, "shelf price must not read as a special"
    on_special = dict(p, promotions=[{"promoId": "1", "rewardType": "NEW_PRICE"}],
                      singlePrice=dict(p["singlePrice"], promoId="1", price=599))
    _, sale = rows_for(on_special, "S1", "2026-09-13")
    assert sale[3] == 599 and sale[6] == 1 and sale[7] == "NEW_PRICE", sale
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
    assert slug("Plain Flour") == "plain_flour"
    assert per_base(148, "100g") == (1.48, "g") and per_base(560, "1kg") == (0.56, "g")
    assert per_base(None, "100g") == (None, None) and per_base(100, "each") == (None, None)
    assert to_base(1.5, "kg") == (1500, "g") and to_base(2, None) == (2, "each")
    assert to_base(3, "large") == (3, "each"), "countable units fall back to each"
    assert to_base(8, None, 5) == (40, "g"), "a known unit weight prices countable things"
    assert to_base(200, "g", 5) == (200, "g"), "a real unit still wins over the weight table"
    assert from_size(149, "1kg") == (0.149, "g") and from_size(259, "44g")[1] == "g"
    assert from_size(699, "12pk") == (699 / 12, "each") and from_size(100, None) == (None, None)
    assert qty_value({"value": {"value": 600.0}}) == 600.0
    assert qty_value({"value": {"value": {"whole": 1, "num": 1, "den": 2}}}) == 1.5
    assert qty_value(None) is None and qty_value({"value": {"value": "a pinch"}}) is None
    # the label after # must not confuse the name, or re-pinning stacks duplicate lines
    assert map_key("spring onion 5001-EA-000  # Fresh Spring Onions") == "spring onion"
    assert map_key("# comment") == "" and map_key("") == ""
    assert words("Onions, Leeks & Shallots") == {"onion", "leek", "shallot"}
    assert words("Soy & Asian Sauces") > words("soy sauce"), "Sauces must singularise to sauce"
    assert singular("tomatoes") == "tomato" and singular("glass") == "glass"
    assert same("g", "ml") and same("each", "each") and not same("g", "each")
    assert cat_score(words("butter"), "Butter & Margarine", "Butter") < \
           cat_score(words("butter"), "Jams, Honey & Spreads", "Peanut & Nut Butter")
    assert cat_score(words("garlic"), "Fresh Salad & Herbs", "Chilli, Garlic & Ginger") < \
           cat_score(words("garlic"), "Burger Buns, Rolls & Garlic Bread", "Burger Buns & Bread Rolls")
    assert cat_score(words("olive oil"), "Oil & Vinegar", "Olive & Avocado Oil") < \
           cat_score(words("olive oil"), "Butter & Margarine", "Dairy Free Spreads")
    assert words("frozen peas") & PRESERVED, "a qualified name must not prefer fresh"
    print("ok")


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    {"sync": cmd_sync, "enrich": cmd_enrich, "compare": cmd_compare, "history": cmd_history,
     "ingredient": cmd_ingredient, "prices": cmd_prices, "basket": cmd_basket, "table": cmd_table, "suggest": cmd_suggest, "apply": cmd_apply,
     "stats": cmd_stats, "selftest": lambda _: demo()}.get(cmd, lambda _: sys.exit(__doc__))(args)

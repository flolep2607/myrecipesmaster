# Recipe vault

Cooklang recipe collection. `cook` CLI (CookCLI) is installed; the `cooklang` plugin
skills wrap it. Base path for every `cook` command is this repo root.

## Layout

```
recipes/            .cook files, subfoldered by course (dinner/, baking/, ...)
config/aisle.conf   shopping-list grouping, ordered like a PAK'nSAVE walk
config/pantry.conf  what's in the kitchen; shopping-list subtracts it automatically
datastore/ingredients/<name>.yaml   nutrition per ingredient (see below)
templates/          Jinja2 report templates for `cook report`
tools/pns.py        PAK'nSAVE API client + one-off price lookup (guest token, no login)
tools/pns_db.py     catalogue + price history in data/paknsave.db (weekly sync)
config/paknsave.stores  store ids, first is the default: Manukau, Royal Oak, Sylvia Park
data/               SQLite DB and sync logs — gitignored, this is the price history, back it up
plans/              weekly meal plans (YYYY-WW.md)
docs/               vendored cooklang spec/conventions/extensions, `./docs/refresh.py` updates them
```

## Workflows

**Import a recipe from a URL** — `cook import <url> > recipes/<course>/<Name>.cook`,
then read it back and fix the parse: ingredients must be `@name{qty%unit}`, cookware `#pan{}`,
timers `~{10%minutes}`. Add frontmatter: `title`, `servings`, `tags`, `source`, `time`.
If `cook import` fails on the site, `./tools/ai_import.py <url> > recipes/<course>/<Name>.cook`
does it with Gemini — same for YouTube links, which it reads as video. Keys go one per line
in `config/gemini.keys` (gitignored); it starts on a random one and rotates past quota errors.
Web pages go through recipe-scrapers first (~660 sites, installed in `.venv`, gitignored —
rebuild with `uv venv --python /usr/bin/python3 .venv && uv pip install --python .venv/bin/python
'recipe-scrapers[online]'`), so the model only writes markup around fields it was handed; a site
recipe-scrapers doesn't know falls back to Gemini reading the page itself.
Once the fields are scraped, writing the markup is plain text work and goes to the free
OpenAI-compatible endpoint in `config/omniroute.key` (model `free`, gitignored); Gemini is kept
for what only it can do — reading a page, watching a video — and for anything the free endpoint
drops. Read the output back and check the parse either way.

**Weekly plan** — write `plans/YYYY-WW.md` listing one recipe per day with its scale
(`Name.cook:2`). Then one shopping list for the whole week:
`cook shopping-list recipes/**/*.cook` with the chosen scales.

**Shopping list** — `cook shopping-list <recipe>:<scale> ... --extra "paper towels"`.
Pantry is subtracted by default; `--ignore-pantry` for the full list. Output grouped by
`config/aisle.conf` so it reads in store order.

**Pantry** — `cook pantry add|remove|update|list`, `cook pantry expiring --days 7`,
`cook pantry recipes` (what can I cook right now). Update it after shopping and after cooking.

**Nutrition** — `cook report -t templates/nutrition.j2 -d datastore <recipe>[:scale]`.
Each ingredient needs `datastore/ingredients/<snake_case_name>.yaml`:
```yaml
# per 100 g/ml, OR per unit for countable things (one egg, one onion)
kcal: 364
protein: 10.3
carbs: 76.3
fat: 1.0
```
The template reports what's missing from the datastore — fill those in as they appear.
Imported recipes often carry schema.org nutrition in their frontmatter; prefer that for
per-recipe totals and use the datastore for hand-written ones.

**Prices** — `./tools/pns.py price butter milk` for named items, or price a whole list:
`cook shopping-list <recipes> --ingredients-only | ./tools/pns.py price -` (prints a total).
`./tools/pns.py stores <town>` lists store ids if the store ever changes.

**Price history** — `./tools/pns_db.py sync` crawls the full catalogue of every configured
store and appends one price row per product per store per day (re-running the same day
overwrites, so it is safe to repeat). Then:
`compare <name>` for today's price at each store cheapest-first, `history <name>` for the
trend, `stats` to check the last run. Takes ~9 min per store.

`./tools/pns_db.py enrich` then fetches the per-product endpoint for anything not yet
detailed, filling `products.ean`, `products.ingredients` and the `nutrition` table
(kcal/protein/carbs/sugars/fat/sat_fat/fibre/sodium per 100g or 100ml, straight from
PAK'nSAVE — no Open Food Facts needed). It skips products already detailed, so the first
run costs ~9700 requests and later runs only cover genuinely new products. Fresh produce
usually has no nutrition and a PLU rather than a real EAN; that is expected, not a bug.
`enrich <n>` details only n products, for a quick check.

Both together, weekly:
`0 7 * * 1  cd ~/recipes && ./tools/pns_db.py sync && ./tools/pns_db.py enrich >> data/sync.log 2>&1`
Prices are store-specific and move weekly — read them from the DB, never copy them into
`datastore/`. Nutrition is the other way round: it is per-product and stable, so the
`nutrition` table is the place to look up a branded ingredient before hand-writing a
`datastore/ingredients/*.yaml` entry.

**Ingredient → product** — `./tools/pns_db.py ingredient "plain flour"` pins the best-matching
catalogue product to a recipe ingredient name, in `config/products.map` (committed, hand-editable).
Matching leans on PAK'nSAVE's own category tree (`Fresh Salad & Herbs > Chilli, Garlic & Ginger`
is what makes `garlic` land on garlic rather than garlic bread), plus the head noun of the
ingredient — `garlic POWDER`, `vegetable OIL` — and prefers fresh produce unless the name says
canned/frozen/dried. Local naming still beats it sometimes: nothing here is called "vegetable oil"
or "ketchup". `-p N` picks another of the listed matches, `-s "other words"` searches different
words than the ingredient is called (`ingredient "reduced sugar ketchup" -s "tomato sauce"`). Each pin writes
`datastore/ingredients/<name>.yaml` (nutrition, only once `enrich` has reached that product) and
`data/prices/ingredients/<name>.yaml` (today's price, gitignored). After a sync, `./tools/pns_db.py
prices` rewrites every price file at once.

**Unit weights** — `config/unit_weights.conf` says what one of a counted ingredient weighs
(`garlic clove 4`, `onion 180`), so `@onion{1}` prices against a product sold by the kilo. Spoons
are converted as volume (tsp 5 ml, tbsp 15 ml, cup 250 ml) and a millilitre is priced as a gram.
`docs/products.md` is the whole mapping as a table — `./tools/pns_db.py table` regenerates it.

**Cost** — `cook report -t templates/cost.j2 -d data/prices <recipe>[:scale]` gives cost per recipe
and per serving. Note `-d data/prices`, not `-d datastore`: prices stay out of the committed
datastore. Weights, volumes and things sold each are priced; a tbsp of oil is not, and the report
lists what it skipped. Products with no shelf unit price fall back to the pack size on the label.

**Ideas from the specials** — `./tools/pns_db.py ideas -n 5 [-t 30]` takes this week's specials at
the default store (cheapest per shelf, a few per aisle, one per product family, nothing that is
already a finished meal), searches recipes.cooklang.org (the Cooklang Federation, 8000+ recipes
already in .cook), BBC Good Food and Marmiton for each, and lists the web ones that come in under
the time limit. Every hit is a real page. Import the ones you like with
`./tools/ai_import.py <url>`; a Federation URL is a plain download, no model involved, unless the
file does not parse here — other people's Cooklang carries Danish spoons and bare `~` meaning
"about" — in which case the recipe is rewritten and the source kept.
`./tools/ai_import.py find "<words>"` searches the web sites and `cook "<words>"` the Federation.

**Google Programmable Search** (optional, if you can still get a key — the JSON endpoint answers
"needs an API key" rather than 404, but new Custom Search API keys may no longer be issued) — one query
across every site recipe-scrapers can parse, instead of scraping two search pages. Create an engine
at programmablesearchengine.google.com, paste the 725 domains in `docs/cse-sites.txt` into "Sites to
search" (regenerate that list with the one-liner in its header), get a Custom Search JSON API key at
console.cloud.google.com, then put the key on the first line of `config/google.cse` and the engine
id (cx) on the second (gitignored). 100 queries a day are free — check current pricing beyond that.
`find` uses it when the file is there and falls back to the six built-in site searches when it is
not, so nothing breaks if the quota runs out: BBC Good Food, BBC Food, Marmiton, Budget Bytes,
RecipeTin Eats and The Woks of Life, all parseable by recipe-scrapers. Prefer sourced recipes; `ai_import.py "<brief>"` writes one from a description, but that is a
fallback for when nothing suitable is online. French sources are fine — imports are written in
English so the names match `config/aisle.conf` and `config/products.map`.

**Which store** — `./tools/pns_db.py basket <recipe>[:scale] ...` totals a whole list at each
configured store, cheapest first. An ingredient that isn't stocked at every store is left out of
every total rather than making one store look cheap.

**Health** — `cook doctor` before committing. `cook server` for a local browsable cookbook.

**Patched CookCLI** — stock cookcli builds its parser with `Extensions::empty()`, so `@&reference`,
`@?optional`, `@-hidden`, `@@other recipe{}` and intermediate preparations all parse as literal names.
`~/src/cookcli-patched/` is 0.35.0 with `cookcli-core/src/parser.rs` flipped to `Extensions::all()`
and cooklang's `bundled_units` feature added to `cookcli-core/Cargo.toml` — without it the converter
knows no units, every timer unit is an error and `500 g` never merges with `0.5 kg` in a shopping
list. `cook update` or a `cargo install cookcli` overwrites it —
re-apply with `cargo install --path ~/src/cookcli-patched/cookcli --locked`, and re-patch the sources
if the version moved.

## Conventions

- Ingredient names: lowercase singular (`egg`, not `Eggs`) so aisle/pantry/datastore all match.
- Every recipe has `servings` in frontmatter — scaling and nutrition depend on it.
- Add new ingredients to `config/aisle.conf` when they land in `[other]` on a shopping list.
- Commit after each import or plan.

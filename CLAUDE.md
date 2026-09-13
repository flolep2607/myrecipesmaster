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
```

## Workflows

**Import a recipe from a URL** — `cook import <url> > recipes/<course>/<Name>.cook`,
then read it back and fix the parse: ingredients must be `@name{qty%unit}`, cookware `#pan{}`,
timers `~{10%minutes}`. Add frontmatter: `title`, `servings`, `tags`, `source`, `time`.
If `cook import` fails on the site, fetch the page and convert by hand.

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

**Health** — `cook doctor` before committing. `cook server` for a local browsable cookbook.

## Conventions

- Ingredient names: lowercase singular (`egg`, not `Eggs`) so aisle/pantry/datastore all match.
- Every recipe has `servings` in frontmatter — scaling and nutrition depend on it.
- Add new ingredients to `config/aisle.conf` when they land in `[other]` on a shopping list.
- Commit after each import or plan.

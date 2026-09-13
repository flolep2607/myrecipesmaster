# Recipe vault

Cooklang recipe collection. `cook` CLI (CookCLI) is installed; the `cooklang` plugin
skills wrap it. Base path for every `cook` command is this repo root.

## Layout

```
recipes/            .cook files, subfoldered by course (dinner/, baking/, ...)
config/aisle.conf   shopping-list grouping, ordered like a PAK'nSAVE walk
config/pantry.conf  what's in the kitchen; shopping-list subtracts it automatically
datastore/<ingredient>/nutrition.yml  nutrition per ingredient (see below)
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
does it with a model — same for YouTube links, which Gemini reads as video. Keys go one per line
in `config/gemini.keys` (gitignored); it starts on a random one and rotates past quota errors.
Web pages go through recipe-scrapers first (~660 sites, installed in `.venv`, gitignored —
rebuild with `uv venv --python /usr/bin/python3 .venv && uv pip install --python .venv/bin/python
'recipe-scrapers[online]'`), so the model only writes markup around fields it was handed; a site
recipe-scrapers doesn't know falls back to Gemini reading the page itself.
Writing the markup is plain text work and goes to the free OpenAI-compatible endpoint in
`config/omniroute.key` (model `free`, gitignored, unlimited), and so is reading a page
recipe-scrapers has no parser for — the page text is fetched and stripped locally. Gemini is kept
for the one thing only it can do, watching a video, and as the fallback when the free endpoint
drops a request. Read the output back and check the parse either way.

**Weekly plan** — write `plans/YYYY-WW.md` listing one recipe per day with its scale
(`Name.cook:2`). Then one shopping list for the whole week:
`cook shopping-list recipes/**/*.cook` with the chosen scales.

**Shopping list** — `cook shopping-list <recipe>:<scale> ... --extra "paper towels"`.
Pantry is subtracted by default; `--ignore-pantry` for the full list. Output grouped by
`config/aisle.conf` so it reads in store order.

**Pantry** — `cook pantry add|remove|update|list`, `cook pantry expiring --days 7`,
`cook pantry recipes` (what can I cook right now). Update it after shopping and after cooking.

**Nutrition** — `cook report -t templates/nutrition.j2 -d datastore <recipe>[:scale]`.
The datastore is one folder per ingredient, named the way `underscore(name)` would, with one
file per topic — the layout https://cooklang.org/guides/reports/ documents, so templates from
cooklang-reports work against it unchanged. Each ingredient needs
`datastore/<snake_case_name>/nutrition.yml`:
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
`datastore/<name>/nutrition.yml` entry.

**Ingredient → product** — `./tools/pns_db.py ingredient "plain flour"` pins the best-matching
catalogue product to a recipe ingredient name, in `config/products.map` (committed, hand-editable).
Matching leans on PAK'nSAVE's own category tree (`Fresh Salad & Herbs > Chilli, Garlic & Ginger`
is what makes `garlic` land on garlic rather than garlic bread), plus the head noun of the
ingredient — `garlic POWDER`, `vegetable OIL` — and prefers fresh produce unless the name says
canned/frozen/dried. Local naming still beats it sometimes: nothing here is called "vegetable oil"
or "ketchup". `-p N` picks another of the listed matches, `-s "other words"` searches different
words than the ingredient is called (`ingredient "reduced sugar ketchup" -s "tomato sauce"`). Each pin writes
`datastore/<name>/nutrition.yml` (only once `enrich` has reached that product) and
`data/prices/<name>/cost.yml` + `shopping.yml` (today's price and the product it came from,
gitignored). After a sync, `./tools/pns_db.py prices` rewrites every price and nutrition file at
once.

**Unit weights** — `config/unit_weights.conf` says what one of a counted ingredient weighs
(`garlic clove 4`, `onion 180`), so `@onion{1}` prices against a product sold by the kilo. Spoons
are converted as volume (tsp 5 ml, tbsp 15 ml, cup 250 ml) and a millilitre is priced as a gram.
`docs/products.md` is the whole mapping as a table — `./tools/pns_db.py table` regenerates it.

**Cost** — `cook report -t templates/cost.j2 -d data/prices <recipe>[:scale]` gives cost per recipe
and per serving. Note `-d data/prices`, not `-d datastore`: prices stay out of the committed
datastore. Weights, volumes and things sold each are priced; a tbsp of oil is not, and the report
lists what it skipped. Products with no shelf unit price fall back to the pack size on the label.

**Basket** — `cook report -t templates/basket.j2 -d data/prices <recipe>[:scale]` lists the
recipe line by line with the PAK'nSAVE product each price came from, linked, and the pack price.
That is what `shopping.yml` is for in the reports guide.

**Ideas from the specials** — `./tools/pns_db.py ideas -n 5 [-t 30]` takes this week's specials at
the default store (cheapest per shelf, a few per aisle, one per product family, nothing that is
already a finished meal) and searches, per special: recipes.cooklang.org (the Cooklang Federation,
8000+ recipes already in .cook), then RecipeRadar, which searches by ingredient rather than by
words and returns the source page, the time and a normalised ingredient list — results are ranked
by how much of each recipe `config/products.map` can already price. The six site searches are the
fallback when RecipeRadar has nothing. `./tools/ai_import.py have tofu "spring onion"` queries
RecipeRadar directly. Every hit is a real page. Import the ones you like with
`./tools/ai_import.py <url>`; a Federation URL is a plain download, no model involved, unless the
file does not parse here — other people's Cooklang carries Danish spoons and bare `~` meaning
"about" — in which case the recipe is rewritten and the source kept.
`./tools/ai_import.py find "<words>"` searches the web sites and `cook "<words>"` the Federation.

Prefer sourced recipes; `ai_import.py "<brief>"` writes one from a description, but that is a
fallback for when nothing suitable is online. French sources are fine — imports are written in
English so the names match `config/aisle.conf` and `config/products.map`. The site searches are
BBC Good Food, BBC Food, Marmiton, Budget Bytes, RecipeTin Eats and The Woks of Life, all
parseable by recipe-scrapers.

**Tags** — `config/tags.conf` is the whole vocabulary, grouped into effort, method, diet and main
ingredient. Imports are handed the list and may use two to five of them; nothing else goes in a
recipe. A tag never repeats what another key says — no `15 minutes` (that's `time:`), no `dinner`
(`course:`), no `asian` (`cuisine:`). `./tools/ai_import.py tags` lists anything in the vault the
vocabulary does not allow; add the tag to the file or retag the recipe.

**Cookware** — `config/cookware.conf` is what the kitchen has: oven, air fryer, slow cooker,
pressure cooker, kettle, pans and the usual bowls. One name per thing, with the other names for it
folded onto the right of the `=` (`frying pan = skillet, non-stick frying pan, pan`), and a
`[missing]` section for gear we do not own — grill, food processor, stand mixer, microwave, deep
fryer. Imports are handed the left-hand names. `./tools/ai_import.py cookware` flags a recipe
calling for gear we lack, using a name the file does not know, or using an alias instead of the
canonical name; a recipe wanting something from `[missing]` needs adapting before it can be cooked.

**Pictures** — `./tools/ai_import.py image "recipes/dinner/Name.cook"` saves the recipe's picture
beside it as `Name.jpg`, which is the convention `cook server` and the apps read. Imports carry an
`image:` url in the frontmatter; older files get one looked up from their `source:` page, and a
YouTube source uses the video thumbnail. Some sites (Marmiton) refuse hotlinks — the url stays in
the file, only the download fails.

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

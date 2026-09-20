#!/usr/bin/env python3
"""Import a recipe into Cooklang with Gemini. Web pages and YouTube videos.

  ./tools/ai_import.py <url> > recipes/dinner/Name.cook
  ./tools/ai_import.py "tempeh egg fried rice, 20 min, tempeh egg rice soy sauce"  # no url: a brief
  ./tools/ai_import.py <url> -m gemini-3.8-pro      # default: gemini-3.8-flash
  ./tools/ai_import.py find "tofu stir fry"   # real recipe urls to import
  ./tools/ai_import.py cook tofu              # search recipes.cooklang.org, already cooklang
  ./tools/ai_import.py have tofu "spring onion"  # recipes built from ingredients you have
  ./tools/ai_import.py tour Japanese          # a cuisine's dishes on TheMealDB, import by id
  ./tools/ai_import.py mealdb:53034           # import one of them, measures and all
  ./tools/ai_import.py search "smoky bean stew"  # semantic search over 50k recipes
  ./tools/ai_import.py brain:"clay pot chicken rice"  # import the top hit from that index
  ./tools/ai_import.py <video-url> --only "Patty Melt"   # one dish out of a compilation video
  ./tools/ai_import.py image "recipes/dinner/Name.cook"   # fetch its picture alongside it
  ./tools/ai_import.py tags                   # tags in use that config/tags.conf does not allow
  ./tools/ai_import.py selftest

Use when `cook import` has no parser for the site, or the source is a video.
Web pages go through recipe-scrapers first (~660 sites, .venv) so the model only
does the markup; sites it doesn't know fall back to Gemini reading the page.

Two providers, split by what only one of them can do. Writing the markup is plain text
work and always goes to the OpenAI-compatible endpoint in config/omniroute.key (model
`free`) — scraped fields, pages we fetch and strip ourselves, TheMealDB, briefs, all of
it, retried if the endpoint is having a bad day. Gemini is for videos, and nothing else.

Keys: config/gemini.keys, one per line (gitignored), or $GEMINI_API_KEYS
comma-separated. A random key starts each run and quota/server errors fall
through to the next one. config/omniroute.key holds the one free-endpoint key,
or $OMNIROUTE_KEY.
"""
import html, json, os, random, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# recipe-scrapers lives in .venv; version-matched so a stale venv is skipped, not imported
sys.path += [str(p) for p in ROOT.glob(".venv/lib/python%d.%d/site-packages" % sys.version_info[:2])]
KEYS_FILE = ROOT / "config/gemini.keys"
OMNI_KEY_FILE = ROOT / "config/omniroute.key"
OMNI = "https://omniroute.masterchef.mom/v1/chat/completions"
# a local OpenAI-compatible proxy, when one is running: no round trip, no quota, ~5s a recipe.
# Model names prefixed `local/` go here instead of omniroute.
LOCAL = os.environ.get("LOCAL_ENDPOINT", "http://localhost:9000") + "/v1/chat/completions"
# Best first; $OMNI_MODELS overrides, comma separated. The local proxy answers in seconds and is
# skipped without complaint when it is not running. `free` is the unlimited one and would belong
# near the front, but it routes through opencode, whose free tier 403s and whose keyed connection
# 401s — a minute of waiting per import — so it sits last until those credentials are sorted.
OMNI_MODELS = [m.strip() for m in (os.environ.get("OMNI_MODELS") or
               "local/swe-2-high, auto/fast, openrouter/openrouter/free, free"
               ).split(",") if m.strip()]
SPEC = ROOT / "docs/extensions.md"
TAGS = ROOT / "config/tags.conf"
COOKWARE = ROOT / "config/cookware.conf"
API = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
ROTATE_ON = {429, 403, 500, 503}

PROMPT = """Convert this recipe into a single Cooklang file. Output ONLY the file, no commentary, no code fences.

Format:
---
title: <dish name>
servings: <number>
course: <breakfast, lunch, dinner, dessert, snack or baking>
cuisine: <e.g. Thai — drop this line if the source does not say>
tags: <two to five, from the list at the bottom, comma separated>
image: <the image url if the source gave one, otherwise drop this line>
source: {url}
prep time: <hands-on time in 1h30m form, no plurals>
cook time: <unattended time, same form>
time: <total, same form — only when the source gives no prep/cook split, never alongside them>
---

Then the steps, one paragraph per step, with ingredients as @name{{qty%unit}},
cookware as #pan{{}} and timers as ~{{10%minutes}}.

Rules:
- Ingredient names lowercase and singular (@egg, @onion), multi-word ones need the braces: @olive oil{{2%tbsp}}.
- Metric units (g, ml, tbsp, tsp), servings always a plain number. One amount per ingredient:
  no ranges and no "1 pinch, 2 tsp" — pick the amount the method actually uses.
- A quantity is {{amount%unit}} and nothing else. Preparation goes in a note after it:
  @rice{{400%g}}(washed), never @rice{{400%g%washed}}.
- Drop any metadata line you have nothing to put on it rather than leaving it empty, and never
  emit a `>> [mode]: ...` line.
- Write the recipe in English even when the source is not — title, steps, ingredient names, every
  word. This is not optional: the aisle file, the pantry and the product map are English, and a
  French ingredient name prices against nothing. Keep the dish's own name if it has one
  (bolognaise, tartiflette).
- Tag every ingredient the first time the method uses it; later mentions are references, @&name{{qty%unit}},
  which add to the first amount. Modifiers @?optional and @-hidden are available too, and
  @./Other Recipe{{2%servings}} references another .cook file in the same folder — never @@name.
- Keep the method wording of the source; do not invent steps or quantities. Guess a quantity only if the source truly omits it.
- If extracted fields are given below, they are authoritative: use those ingredients, amounts and steps, and do not add any.

Tags — use only these, two to five of them, and nothing that `course`, `cuisine` or `time`
already says:
{tags}

Cookware — this kitchen has these and nothing else, so call the equipment by these
names and do not reach for anything that is not here:
{cookware}

Cooklang syntax reference:
{spec}

Source: {url}
{data}"""


def keys():
    raw = os.environ.get("GEMINI_API_KEYS") or (KEYS_FILE.read_text() if KEYS_FILE.exists() else "")
    ks = [k for k in re.split(r"[\s,]+", raw) if k and not k.startswith("#")]
    if not ks:
        sys.exit(f"No API keys. Put one per line in {KEYS_FILE} or set $GEMINI_API_KEYS.")
    random.shuffle(ks)
    return ks


def scrape(url):
    """Recipe fields from recipe-scrapers, or None if it has no parser for this site."""
    try:
        from recipe_scrapers import scrape_me
        r = scrape_me(url)
        got = {}
        for f in ("title", "yields", "total_time", "prep_time", "cook_time", "cuisine",
                  "category", "image", "ingredients", "instructions"):
            try:
                got[f] = getattr(r, f)()
            except Exception:
                pass  # optional fields raise when the page omits them
        return json.dumps(got, indent=1) if got.get("ingredients") else None
    except Exception as e:
        print(f"recipe-scrapers: {type(e).__name__}, letting gemini read the page", file=sys.stderr)
        return None


# plain HTML search pages, all known to recipe-scrapers: (search url, recipe url pattern).
# The WordPress ones (?s=) also return roundups and about pages; scrape() drops those.
SITES = [("https://www.bbcgoodfood.com/search?q=%s",
          r"https://www\.bbcgoodfood\.com/recipes/[a-z0-9-]+"),
         ("https://www.marmiton.org/recettes/recherche.aspx?aqt=%s",
          r"https://www\.marmiton\.org/recettes/recette_[a-z0-9_-]+\.aspx"),
         ("https://www.bbc.co.uk/food/search?q=%s",
          r"https://www\.bbc\.co\.uk/food/recipes/[a-z0-9_]+"),
         ("https://www.budgetbytes.com/?s=%s", r"https://www\.budgetbytes\.com/[a-z0-9-]+/"),
         ("https://www.recipetineats.com/?s=%s", r"https://www\.recipetineats\.com/[a-z0-9-]+/"),
         ("https://thewoksoflife.com/?s=%s", r"https://thewoksoflife\.com/[a-z0-9-]+/")]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36"}


FED = "https://recipes.cooklang.org"   # the Cooklang Federation: 8000+ recipes already in .cook


def fetch(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf8", "ignore")


def federation(query, limit=10):
    """Search the Federation. These are already Cooklang, so importing one is a download —
    nothing to convert, nothing for a model to invent."""
    page = fetch(f"{FED}/?q={urllib.parse.quote(query)}")   # `html` is the stdlib module here
    cards = re.findall(r'href="/recipes/(\d+)".*?<h3[^>]*>(.*?)</h3>', page, re.S)
    out, seen = [], set()
    for rid, title in cards:
        title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip())
        if rid not in seen:
            seen.add(rid)
            out.append((f"{FED}/recipes/{rid}", title))
    return out[:limit]


MEALDB = "https://www.themealdb.com/api/json/v1/1"   # ~200 cuisines, real measures, a source page
BRAIN = "https://recipes.aidatanorge.no/mcp"          # semantic search over 50k recipes, via MCP


def mealdb(mid):
    """One TheMealDB meal as the fields block the prompt takes. The measures are there, so this
    is the same plain-text job as a scraped page: no page to read, nothing to invent."""
    m = json.loads(fetch(f"{MEALDB}/lookup.php?i={mid}"))["meals"][0]
    ing = [f"{(m[f'strMeasure{i}'] or '').strip()} {m[f'strIngredient{i}'].strip()}".strip()
           for i in range(1, 21) if (m.get(f"strIngredient{i}") or "").strip()]
    return {"title": m["strMeal"], "cuisine": m["strArea"], "course": m["strCategory"],
            "image": m["strMealThumb"], "ingredients": ing, "instructions": m["strInstructions"],
            "source": m["strSource"] or m["strYoutube"] or f"https://www.themealdb.com/meal/{mid}"}


def tour(area):
    """What TheMealDB holds for one cuisine — `tour Japanese`, `tour Moroccan`."""
    meals = json.loads(fetch(f"{MEALDB}/filter.php?a={urllib.parse.quote(area)}")).get("meals")
    return [(m["idMeal"], m["strMeal"]) for m in (meals or [])]


def brain(query, limit=6):
    """Semantic search over 50k recipes. Ingredients come back without amounts, so a hit is a
    lead: the real recipe is on the source page the record names."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_recipes", "arguments": {"query": query, "limit": limit}}}
    req = urllib.request.Request(BRAIN, data=json.dumps(body).encode(),
                                 headers={**UA, "Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    # fastmcp answers as one server-sent event whose data is the JSON-RPC reply
    hit = json.loads(raw.split("data: ", 1)[-1])["result"]["content"][0]["text"]
    return json.loads(hit)


RADAR = "https://www.reciperadar.com/api/recipes/search"   # openculinary, search by ingredient


def radar(ingredients, limit=5, max_time=None):
    """RecipeRadar searches by ingredient rather than words, which is the question we actually
    have: what can I cook from this. Results carry the source page, the time and a normalised
    ingredient list."""
    query = urllib.parse.urlencode([("ingredients[]", i) for i in ingredients])
    try:
        found = json.loads(fetch(f"{RADAR}?{query}")).get("results", [])
    except Exception as e:
        print(f"reciperadar: {type(e).__name__}", file=sys.stderr)
        return []
    out = []
    for r in found:
        if max_time and (r.get("time") or 10 ** 4) > max_time:
            continue
        out.append({"title": r["title"], "url": r["dst"], "time": r.get("time"),
                    "ingredients": [i["product"]["id"] for i in r.get("ingredients", [])]})
    return out[:limit]


def recipe_slug(url):
    """WordPress search pages link their own plumbing — /feed/, /wp-includes/, /tachyon/ — beside
    the recipes. A recipe permalink there is a multi-word slug ending in a slash."""
    if not url.endswith("/"):
        return True                       # bbcgoodfood, bbc.co.uk and marmiton patterns are exact
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.count("-") >= 2 and not slug.startswith("wp-")


def find(query, limit=3):
    """Real recipe URLs for a search term, a few from each site. Pages that exist, rather than a
    model's memory of one."""
    out = []
    for search, pattern in SITES:
        req = urllib.request.Request(search % urllib.parse.quote(query), headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf8", "ignore")
        except Exception as e:
            print(f"search for {query!r}: {type(e).__name__}", file=sys.stderr)
            continue
        urls = [u for u in dict.fromkeys(re.findall(pattern, html))
                if recipe_slug(u)]
        out += urls[:limit]
    return out


def page_text(url, cap=24000):
    """The readable text of a page. recipe-scrapers covers 725 sites; for the rest this plus the
    free endpoint beats spending a Gemini key on url_context."""
    try:
        page = fetch(url)
    except Exception as e:
        print(f"fetch {url}: {type(e).__name__}", file=sys.stderr)
        return None
    page = re.sub(r"(?s)<(script|style|nav|footer|header)\b.*?</\1>", " ", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return re.sub(r"[ \t]*\n\s*", "\n", re.sub(r"[ \t]+", " ", text)).strip()[:cap] or None


def image(cook_file):
    """Download the recipe's image next to it, as the conventions want: Baked Potato.cook +
    Baked Potato.jpg. Apps and `cook server` pick it up from there."""
    path = Path(cook_file)
    text = path.read_text()
    found = re.search(r"^image:\s*(\S+)", text, re.M)
    if not found:      # older imports have no image line: ask the source for one
        src = re.search(r"^source:\s*(https?://\S+)", text, re.M)
        vid = re.search(r"(?:youtu\.be/|v=|shorts/)([\w-]{11})", src[1]) if src else None
        if vid:
            url = f"https://img.youtube.com/vi/{vid[1]}/maxresdefault.jpg"
        else:
            url = (json.loads(scrape(src[1]) or "{}") if src else {}).get("image")
        if not url:
            return print(f"{path.name}: no image to fetch", file=sys.stderr)
        path.write_text(text.replace("\nsource:", f"\nimage: {url}\nsource:", 1))
    url = re.search(r"^image:\s*(\S+)", path.read_text(), re.M)[1]
    ext = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
    out = path.with_suffix(ext)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            out.write_bytes(r.read())
    except Exception as e:      # Marmiton and friends refuse hotlinks; the url stays in the file
        return print(f"{path.name}: {type(e).__name__} fetching the image", file=sys.stderr)
    print(f"{out.name}  {out.stat().st_size // 1024} KB")


def tag_vocab():
    return [w for line in TAGS.read_text().splitlines()
            for w in [line.split("#")[0].strip()] if w and not w.startswith("[")]


def cookware_vocab():
    """{name or alias: the name a recipe should use}, the names we do not own, and the ones
    that live somewhere else — a park barbecue is cookable, it just needs leaving the house."""
    canon, missing, away, section = {}, set(), set(), ""
    for line in COOKWARE.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("["):
            section = line.strip("[]")
        elif line:
            name, _, aliases = line.partition("=")
            name = name.strip()
            canon.update({a: name for a in [name] + [x.strip() for x in aliases.split(",") if x.strip()]})
            if section == "missing":
                missing.add(name)
            elif section == "away":
                away.add(name)
    cookware_vocab.away = away
    return canon, missing


def cookware_used(text):
    return {(a or b).strip().lower()
            for a, b in re.findall(r"#&?([^{@~#\n]+)\{[^}]*\}|#&?([A-Za-z-]+)", text)}


def audit_cookware():
    """Cookware the vault names that config/cookware.conf does not know, or does not have."""
    canon, missing = cookware_vocab()
    away = cookware_vocab.away
    unknown, absent, alias, out = {}, {}, {}, {}
    for f in sorted(ROOT.glob("recipes/*/*.cook")):
        for item in cookware_used(f.read_text()):
            name = canon.get(item)
            bucket = (unknown if name is None else absent if name in missing
                      else out if name in away else alias if name != item else {})
            bucket.setdefault(item, []).append(f.stem)
    for label, found in (("not in config/cookware.conf", unknown), ("we do not have", absent),
                         ("a trip to a public barbecue", out),
                         ("another name for one we have", alias)):
        for item, files in sorted(found.items(), key=lambda kv: -len(kv[1])):
            print(f"{item:22} {label:26} {', '.join(sorted(set(files)))}")
    if not unknown and not absent and not alias and not out:
        print("every recipe cooks with gear we have")


def audit_tags():
    """Every tag in the vault that config/tags.conf does not allow."""
    allowed, used = set(tag_vocab()), {}
    for f in sorted(ROOT.glob("recipes/*/*.cook")):
        found = re.search(r"^tags:\s*(.+)$", f.read_text(), re.M)
        for tag in (t.strip().lower() for t in (found[1] if found else "").split(",")):
            if tag and tag not in allowed:
                used.setdefault(tag, []).append(f.stem)
    for tag, files in sorted(used.items(), key=lambda kv: -len(kv[1])):
        print(f"{tag:20} {len(files)}  {', '.join(files[:3])}")
    print(f"{len(used)} tags outside the vocabulary" if used else "every tag is in the vocabulary")


def render(obj):
    """A JSON recipe into a .cook file. Python writes the frontmatter, so servings is a number,
    the times are in cooklang's form and a tag the vocabulary does not know never lands."""
    allowed, out = set(tag_vocab()), []
    keys = [("title", "title"), ("servings", "servings"), ("course", "course"),
            ("cuisine", "cuisine"), ("tags", "tags"), ("prep time", "prep_time"),
            ("cook time", "cook_time"), ("time", "time"), ("image", "image")]
    for name, key in keys:
        v = obj.get(key) if obj.get(key) is not None else obj.get(name)
        if v in (None, "", [], {}):
            continue
        if name == "tags":
            v = [t for t in (v if isinstance(v, list) else str(v).split(",")) if t.strip() in allowed]
            if not v:
                continue
            v = ", ".join(t.strip() for t in v)
        out.append(f"{name}: {v}")
    steps = obj.get("steps") or []
    body = "\n\n".join(s.strip() for s in steps if s and s.strip())
    return "---\n" + "\n".join(out) + "\n---\n\n" + body + "\n"


def structured(url, data, header="Fields extracted from the page"):
    """Ask for JSON and build the file here. None when the model will not produce JSON, which
    puts the caller back on the plain-text path."""
    raw = omni(prompt(url, data, header), as_json=True)
    if not raw:
        return None
    try:
        obj = json.loads(unfence(raw))
    except ValueError:
        print("model did not return json, writing the file the long way", file=sys.stderr)
        return None
    if not obj.get("steps"):
        return None
    obj.setdefault("source", url)
    text = tidy(render(obj))
    text = text.replace("---\n\n", f"source: {url}\n---\n\n", 1) if "source:" not in text else text
    return text if parses(text) else None


def tidy(text):
    """The three things every model gets wrong, fixed without asking one: a count written as a
    unit, minutes spelled long in the time keys, and multi-word cookware left unbraced — `#rice
    cooker` parses as `#rice` followed by the word `cooker`."""
    text = re.sub(r"\{(\d+(?:\.\d+)?)%each\}", r"{\1}", text)
    text = re.sub(r"^((?:prep |cook )?time):\s*(\d+)\s*min(?:ute)?s?\b", r"\1: \2m", text, flags=re.M)
    for name in sorted(set(cookware_vocab()[0].values()), key=len, reverse=True):
        if " " in name:
            text = re.sub(r"#" + re.escape(name) + r"(?!\{)", f"#{name}{{}}", text)
    return text


def parses(text):
    """Does our CookCLI accept this file? Shared recipes carry other people's units and habits."""
    out = subprocess.run(["cook", "recipe", "-f", "json"], input=text,
                         capture_output=True, text=True, cwd=ROOT)
    return out.returncode == 0


def is_url(s):
    return s.startswith(("http://", "https://"))


def prompt(url, data, header="Fields extracted from the page"):
    canon, missing = cookware_vocab()
    return PROMPT.format(url=url, spec=SPEC.read_text(), tags=" ".join(tag_vocab()),
                         cookware=", ".join(sorted(set(canon.values()) - missing)),
                         data=f"\n{header}:\n{data}\n" if data else "")


def body(url, data=None):
    """YouTube goes in as video. A scraped page ships its fields; anything else
    is read by the url_context tool."""
    text = prompt(url, data)
    if re.search(r"(youtube\.com|youtu\.be)/", url):
        return {"contents": [{"parts": [{"file_data": {"file_uri": url}}, {"text": text}]}]}
    if data:
        return {"contents": [{"parts": [{"text": text}]}]}
    return {"contents": [{"parts": [{"text": text}]}], "tools": [{"url_context": {}}]}


JSON_SYS = ("You return only one JSON object and nothing else — no prose, no code fence. "
            "Keys: title (string), servings (integer), course, cuisine, tags (array of strings), "
            "prep_time, cook_time, image, steps (array of strings, each one step of the method "
            "written in Cooklang). Leave out any key the source does not give. "
            "The markup rules in the user message apply to the strings in steps. "
            "Write everything in English — title, steps, ingredient names — whatever language "
            "the source is in. This is not optional.")


def omni(text, model=None, as_json=False):
    """An OpenAI-compatible endpoint: the local proxy for `local/` models, omniroute otherwise.
    `as_json` asks for one JSON object instead of a file — json_schema is accepted and then
    ignored here, but json_object plus a system message holds. None when there is no answer."""
    model = model or OMNI_MODELS[0]
    local = model.startswith("local/")
    key = "local" if local else (os.environ.get("OMNIROUTE_KEY") or
                                 (OMNI_KEY_FILE.read_text().strip() if OMNI_KEY_FILE.exists() else ""))
    if not key:
        return None
    req = urllib.request.Request(LOCAL if local else OMNI, data=json.dumps(
        {"model": model.split("local/")[-1], "stream": False,   # newer omniroute streams by default
         **({"response_format": {"type": "json_object"}} if as_json else {}),
         "messages": ([{"role": "system", "content": JSON_SYS}] if as_json else [])
                     + [{"role": "user", "content": text}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        # 3 minutes is generous for one page of markup; past that the endpoint is having a day
        # and Gemini will answer faster than waiting out a 10-minute socket
        with urllib.request.urlopen(req, timeout=180) as r:
            msg = json.load(r)["choices"][0]["message"]
        # some routed models answer with an empty content and put the text in reasoning
        return (msg.get("content") or msg.get("reasoning_content") or "").strip() or None
    except Exception as e:
        print(f"{model}: {type(e).__name__}", file=sys.stderr)
        return None


def gemini(payload, model):
    """POST to Gemini, walking the keys past quota and overload."""
    payload = json.dumps(payload).encode()
    last = None
    for i, key in enumerate(keys()):
        req = urllib.request.Request(API % model, data=payload,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        for attempt in range(3):          # 503 means overloaded, and it clears in seconds
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code not in ROTATE_ON:
                    sys.exit(f"gemini {e.code}: {e.read().decode()[:500]}")
                last = f"{e.code} on key {i + 1}"
                if e.code != 503 or attempt == 2:
                    print(f"key {i + 1}: {e.code}, trying next", file=sys.stderr)
                    break
                print(f"key {i + 1}: 503, retrying in {4 * (attempt + 1)}s", file=sys.stderr)
                time.sleep(4 * (attempt + 1))
    sys.exit(f"all keys exhausted ({last})")


def clean(resp):
    parts = resp["candidates"][0].get("content", {}).get("parts", [])
    return "".join(p["text"] for p in parts if "text" in p).strip()


def unfence(text):
    text = re.sub(r"\A```[a-z]*\n|\n```\Z", "", text.strip()).strip()
    # some models start straight at `title:` and only close the frontmatter
    if re.match(r"^(title|servings|course|cuisine|tags|source|image|prep time):", text) \
            and re.search(r"^---$", text, re.M):
        text = "---\n" + text
    return text


def free(text, rounds=2):
    """Everything that is not a video is the free endpoint's job — gemini is for videos only —
    so a bad afternoon there is a wait and a different model, never a failover to a keyed one.
    One model at a time, best first, then round again after a pause."""
    for n in range(rounds):
        for model in OMNI_MODELS:
            out = omni(text, model)
            if out:
                if model != OMNI_MODELS[0]:
                    print(f"written by {model}", file=sys.stderr)
                return out
        if n + 1 < rounds:
            time.sleep(10 * (n + 1))
    sys.exit(f"none of {', '.join(OMNI_MODELS)} answered — try again later "
             "(gemini is for videos only)")


def recipe(url, model, only=None):
    """Writing the markup is plain text work and goes to the free endpoint, always.
    Gemini is for one thing: watching a video. A brief instead of a URL is text work too."""
    fed = re.match(rf"{FED}/recipes/(\d+)", url)
    if fed or url.endswith(".cook"):   # already Cooklang: take the file as written
        text = fetch(f"{FED}/api/recipes/{fed[1]}/download" if fed else url)
        # other people write "~1 cm" meaning roughly; our parser reads a bare ~ as a timer
        text = re.sub(r"(?<!\\)~(?![^\n{]{0,20}\{)", r"\\~", text)
        if not re.search(r"^source:", text, re.M):
            text = f"---\nsource: {url}\n---\n\n{text}"
        if parses(text):
            return text
        # someone else's Cooklang, in Danish with Danish spoons: keep the recipe, redo the markup
        print("does not parse here — rewriting it", file=sys.stderr)
        return tidy(unfence(free(prompt(url, text, "Recipe to rewrite, keeping every step and amount")))) + "\n"
    if url.startswith("brain:"):
        # the semantic index carries the whole recipe but no amounts, so the writer supplies them
        hits = brain(url[6:], limit=1)
        if not hits:
            sys.exit("nothing in the index for that")
        r = hits[0]
        r["source_url"] = f"https://www.{r['source']}/recipe/{r['recipe_id']}"
        note = ("The source lists ingredients without amounts: give each one a sensible amount for "
                "the servings, and say so in a `-- ` comment under the frontmatter.")
        data = json.dumps(r, indent=1) + "\n" + note
        return (structured(r["source_url"], data, "Recipe from a recipe index")
                or tidy(unfence(free(prompt(r["source_url"], data, "Recipe from a recipe index")))) + "\n")
    meal = re.match(r"(?:mealdb:|https?://(?:www\.)?themealdb\.com/meal/)(\d+)", url)
    if meal:   # TheMealDB hands over measures and steps, so this is markup work, not reading
        data = mealdb(meal[1])
        return tidy(unfence(free(prompt(data["source"], json.dumps(data, indent=1),
                                        "Fields from TheMealDB")))) + "\n"
    if not is_url(url):   # a brief, not a page: the model writes the recipe from it
        return tidy(unfence(free(prompt("kitchen idea", url, "What to cook")))) + "\n"
    if re.search(r"(youtube\.com|youtu\.be)/", url):   # the one thing only gemini can do
        # a compilation video holds several recipes and the prompt asks for one file, so the
        # model writes the first and stops: --only names which one to write
        text = unfence(clean(gemini(body(url, only and f"Write only the recipe for: {only}. "
                                         "Ignore every other dish in the video."), model)))
        if not text:
            sys.exit("gemini returned nothing for this video")
        return tidy(text) + "\n"
    # recipe-scrapers knows the site, or we fetch and strip the page ourselves — either way the
    # fields arrive here as text and the endpoint writes the markup, as JSON when it will
    data = scrape(url) or page_text(url)
    return structured(url, data) or tidy(unfence(free(prompt(url, data)))) + "\n"


def selftest():
    assert [k for k in re.split(r"[\s,]+", "a, b\nc") if k] == ["a", "b", "c"]
    assert "file_data" in json.dumps(body("https://youtu.be/x"))
    assert "url_context" in json.dumps(body("https://example.com/r"))
    assert is_url("https://x/y") and not is_url("tempeh fried rice, 20 min")
    assert recipe_slug("https://www.budgetbytes.com/vegan-tofu-stir-fry/")
    assert not recipe_slug("https://www.budgetbytes.com/feed/")
    assert not recipe_slug("https://www.recipetineats.com/wp-includes/")
    assert recipe_slug("https://www.marmiton.org/recettes/recette_brownies_16951.aspx")
    scraped = json.dumps(body("https://example.com/r", '{"ingredients": ["1 egg"]}'))
    assert "url_context" not in scraped and "1 egg" in scraped
    assert clean({"candidates": [{"content": {"parts": [{"text": " @egg{1} "}]}}]}) == "@egg{1}"
    assert unfence("```cooklang\n@egg{1}\n```") == "@egg{1}"
    assert unfence("title: T\n---\n\n@egg{1}").startswith("---\ntitle: T")
    assert tidy("prep time: 15min\nPut it in the #rice cooker, add @carrot{1%each}.") == \
        "prep time: 15m\nPut it in the #rice cooker{}, add @carrot{1}."
    card = render({"title": "T", "servings": "2", "tags": ["quick", "not-a-tag"],
                   "prep_time": "10m", "steps": ["Fry @egg{1}.", "Serve."]})
    assert card.startswith("---\ntitle: T\nservings: 2\n") and "tags: quick\n" in card
    assert card.endswith("Fry @egg{1}.\n\nServe.\n") and "not-a-tag" not in card
    assert cookware_used("a #skillet{} then #oven{} and #kettle and simmer") == {"skillet", "oven", "kettle"}
    canon, missing = cookware_vocab()
    assert canon["instant pot"] == "pressure cooker" and "food processor" in missing
    assert canon["bbq"] == "grill" and "grill" in cookware_vocab.away
    print("ok")


if __name__ == "__main__":
    only = None
    if "--only" in sys.argv:
        i = sys.argv.index("--only")
        only = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    args = [a for a in sys.argv[1:] if a != "-m"]
    if args[:1] == ["selftest"]:
        selftest()
    elif args[:1] == ["find"]:
        print("\n".join(find(" ".join(args[1:]))))
    elif args[:1] == ["tags"]:
        audit_tags()
    elif args[:1] == ["cookware"]:
        audit_cookware()
    elif args[:1] == ["image"]:
        image(args[1])
    elif args[:1] == ["have"]:
        for r in radar(args[1:], limit=8):
            print(f"{r['time'] or '?':>4} min  {r['title'][:44]:46} {r['url']}")
    elif args[:1] == ["tour"]:
        for mid, name in tour(" ".join(args[1:])):
            print(f"  mealdb:{mid}  {name}")
    elif args[:1] == ["search"]:
        for r in brain(" ".join(args[1:])):
            src = f"{r.get('source', '?')}/{r.get('recipe_id', '')}"
            print(f"{(r.get('total_time') or '?'):>4} min  {r['title'][:46]:48} {src}")
    elif args[:1] == ["cook"]:
        for u, title in federation(" ".join(args[1:])):
            print(f"{title}\n  {u}")
    elif not args:
        sys.exit(__doc__)
    else:
        sys.stdout.write(recipe(args[0], args[1] if len(args) > 1 else "gemini-3.8-flash", only))

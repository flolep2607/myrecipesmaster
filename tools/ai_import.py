#!/usr/bin/env python3
"""Import a recipe into Cooklang with Gemini. Web pages and YouTube videos.

  ./tools/ai_import.py <url> > recipes/dinner/Name.cook
  ./tools/ai_import.py "tempeh egg fried rice, 20 min, tempeh egg rice soy sauce"  # no url: a brief
  ./tools/ai_import.py <url> -m gemini-3.8-pro      # default: gemini-3.8-flash
  ./tools/ai_import.py find "tofu stir fry"   # real recipe urls to import
  ./tools/ai_import.py cook tofu              # search recipes.cooklang.org, already cooklang
  ./tools/ai_import.py have tofu "spring onion"  # recipes built from ingredients you have
  ./tools/ai_import.py image "recipes/dinner/Name.cook"   # fetch its picture alongside it
  ./tools/ai_import.py tags                   # tags in use that config/tags.conf does not allow
  ./tools/ai_import.py selftest

Use when `cook import` has no parser for the site, or the source is a video.
Web pages go through recipe-scrapers first (~660 sites, .venv) so the model only
does the markup; sites it doesn't know fall back to Gemini reading the page.

Two providers, because only one of them can do the hard half: once recipe-scrapers
has the fields, writing markup is plain text work and goes to the OpenAI-compatible
endpoint in config/omniroute.key (model `free`). Reading a page or watching a video
needs Gemini, so YouTube links and unscrapable sites go there, and anything the free
endpoint fumbles falls back to it too.

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
OMNI_MODEL = "free"
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
time: <1h30m form, no plurals>
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
- Write the recipe in English even when the source is not: the aisle file, the pantry and the
  product map are English. Keep the dish's own name if it has one (bolognaise, tartiflette).
- Tag every ingredient the first time the method uses it; later mentions are references, @&name{{qty%unit}},
  which add to the first amount. Modifiers @?optional, @-hidden and @@other recipe{{}} are available too.
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
        for f in ("title", "yields", "total_time", "cuisine", "category", "image",
                  "ingredients", "instructions"):
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
    """{name or alias: the name a recipe should use}, and the names we do not own."""
    canon, missing, section = {}, set(), ""
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
    return canon, missing


def cookware_used(text):
    return {(a or b).strip().lower()
            for a, b in re.findall(r"#&?([^{@~#\n]+)\{[^}]*\}|#&?([A-Za-z-]+)", text)}


def audit_cookware():
    """Cookware the vault names that config/cookware.conf does not know, or does not have."""
    canon, missing = cookware_vocab()
    unknown, absent, alias = {}, {}, {}
    for f in sorted(ROOT.glob("recipes/*/*.cook")):
        for item in cookware_used(f.read_text()):
            name = canon.get(item)
            bucket = unknown if name is None else absent if name in missing else alias if name != item else {}
            bucket.setdefault(item, []).append(f.stem)
    for label, found in (("not in config/cookware.conf", unknown), ("we do not have", absent),
                         ("another name for one we have", alias)):
        for item, files in sorted(found.items(), key=lambda kv: -len(kv[1])):
            print(f"{item:22} {label:26} {', '.join(sorted(set(files)))}")
    if not unknown and not absent and not alias:
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


def omni(text):
    """The free OpenAI-compatible endpoint. None when it has no key or no answer."""
    key = os.environ.get("OMNIROUTE_KEY") or (OMNI_KEY_FILE.read_text().strip() if OMNI_KEY_FILE.exists() else "")
    if not key:
        return None
    req = urllib.request.Request(OMNI, data=json.dumps(
        {"model": OMNI_MODEL, "messages": [{"role": "user", "content": text}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip() or None
    except Exception as e:
        print(f"free endpoint: {type(e).__name__}, falling back to gemini", file=sys.stderr)
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


def google(query, model="gemini-3.8-flash"):
    """Gemini with Search grounding. Use it when you need pages that exist, not remembered ones."""
    return clean(gemini({"contents": [{"parts": [{"text": query}]}],
                         "tools": [{"google_search": {}}]}, model))


def clean(resp):
    parts = resp["candidates"][0].get("content", {}).get("parts", [])
    return "".join(p["text"] for p in parts if "text" in p).strip()


def unfence(text):
    return re.sub(r"\A```[a-z]*\n|\n```\Z", "", text.strip()).strip()


def recipe(url, model):
    """Scraped fields are plain text work for the free endpoint; reading the page
    or the video is Gemini's job, and so is anything the free endpoint drops.
    A brief instead of a URL is plain text work too — the model writes the recipe."""
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
        return unfence(omni(prompt(url, text, "Recipe to rewrite, keeping every step and amount"))
                       or "") + "\n"
    if not is_url(url):   # a brief, not a page: the model writes the recipe from it
        return unfence(omni(prompt("kitchen idea", url, "What to cook")) or "") + "\n"
    video = re.search(r"(youtube\.com|youtu\.be)/", url)
    data = None if video else scrape(url)
    if not (data or video):
        data = page_text(url)   # recipe-scrapers has no parser for this site: read it ourselves
    text = omni(prompt(url, data)) if data else None
    text = text or clean(gemini(body(url, data), model))
    text = unfence(text)
    if not text:
        sys.exit("empty response from both providers")
    return text + "\n"


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
    assert cookware_used("a #skillet{} then #oven{} and #kettle and simmer") == {"skillet", "oven", "kettle"}
    canon, missing = cookware_vocab()
    assert canon["instant pot"] == "pressure cooker" and "grill" in missing
    print("ok")


if __name__ == "__main__":
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
    elif args[:1] == ["cook"]:
        for u, title in federation(" ".join(args[1:])):
            print(f"{title}\n  {u}")
    elif not args:
        sys.exit(__doc__)
    else:
        sys.stdout.write(recipe(args[0], args[1] if len(args) > 1 else "gemini-3.8-flash"))

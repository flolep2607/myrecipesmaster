#!/usr/bin/env python3
"""Import a recipe into Cooklang with Gemini. Web pages and YouTube videos.

  ./tools/ai_import.py <url> > recipes/dinner/Name.cook
  ./tools/ai_import.py "tempeh egg fried rice, 20 min, tempeh egg rice soy sauce"  # no url: a brief
  ./tools/ai_import.py <url> -m gemini-3.8-pro      # default: gemini-3.8-flash
  ./tools/ai_import.py find "tofu stir fry"   # real recipe urls to import
  ./tools/ai_import.py cook tofu              # search recipes.cooklang.org, already cooklang
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
import html, json, os, random, re, subprocess, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# recipe-scrapers lives in .venv; version-matched so a stale venv is skipped, not imported
sys.path += [str(p) for p in ROOT.glob(".venv/lib/python%d.%d/site-packages" % sys.version_info[:2])]
KEYS_FILE = ROOT / "config/gemini.keys"
OMNI_KEY_FILE = ROOT / "config/omniroute.key"
OMNI = "https://omniroute.masterchef.mom/v1/chat/completions"
OMNI_MODEL = "free"
SPEC = ROOT / "docs/extensions.md"
API = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
ROTATE_ON = {429, 403, 500, 503}

PROMPT = """Convert this recipe into a single Cooklang file. Output ONLY the file, no commentary, no code fences.

Format:
---
title: <dish name>
servings: <number>
course: <breakfast, lunch, dinner, dessert, snack or baking>
cuisine: <e.g. Thai — drop this line if the source does not say>
tags: <comma, separated>
source: {url}
time: <1h30m form, no plurals>
---

Then the steps, one paragraph per step, with ingredients as @name{{qty%unit}},
cookware as #pan{{}} and timers as ~{{10%minutes}}.

Rules:
- Ingredient names lowercase and singular (@egg, @onion), multi-word ones need the braces: @olive oil{{2%tbsp}}.
- Metric units (g, ml, tbsp, tsp), servings always a plain number. One amount per ingredient:
  no ranges and no "1 pinch, 2 tsp" — pick the amount the method actually uses.
- Drop any metadata line you have nothing to put on it rather than leaving it empty, and never
  emit a `>> [mode]: ...` line.
- Write the recipe in English even when the source is not: the aisle file, the pantry and the
  product map are English. Keep the dish's own name if it has one (bolognaise, tartiflette).
- Tag every ingredient the first time the method uses it; later mentions are references, @&name{{qty%unit}},
  which add to the first amount. Modifiers @?optional, @-hidden and @@other recipe{{}} are available too.
- Keep the method wording of the source; do not invent steps or quantities. Guess a quantity only if the source truly omits it.
- If extracted fields are given below, they are authoritative: use those ingredients, amounts and steps, and do not add any.

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
        for f in ("title", "yields", "total_time", "cuisine", "category", "ingredients", "instructions"):
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
CSE_FILE = ROOT / "config/google.cse"   # two lines: api key, then the engine id (cx)
CSE = "https://www.googleapis.com/customsearch/v1"


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


def cse(query, limit=8):
    """Google Programmable Search across every site in docs/cse-sites.txt. 100 queries a day free.
    Returns [] when it is not configured, and the two built-in site searches take over."""
    if not CSE_FILE.exists():
        return []
    parts = CSE_FILE.read_text().split()
    if len(parts) < 2:
        return []
    key, cx = parts[0], parts[1]
    try:
        r = json.loads(fetch(f"{CSE}?key={key}&cx={cx}&num={min(limit, 10)}"
                             f"&q={urllib.parse.quote(query)}"))
    except Exception as e:
        print(f"google cse: {type(e).__name__} — falling back to site search", file=sys.stderr)
        return []
    return [i["link"] for i in r.get("items", [])][:limit]


def recipe_slug(url):
    """WordPress search pages link their own plumbing — /feed/, /wp-includes/, /tachyon/ — beside
    the recipes. A recipe permalink there is a multi-word slug ending in a slash."""
    if not url.endswith("/"):
        return True                       # bbcgoodfood, bbc.co.uk and marmiton patterns are exact
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.count("-") >= 2 and not slug.startswith("wp-")


def find(query, limit=3):
    """Real recipe URLs for a search term. Google Programmable Search when it is set up, the two
    built-in site searches otherwise. Pages that exist, rather than a model's memory of one."""
    out = cse(query, limit)
    if out:
        return out
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


def parses(text):
    """Does our CookCLI accept this file? Shared recipes carry other people's units and habits."""
    out = subprocess.run(["cook", "recipe", "-f", "json"], input=text,
                         capture_output=True, text=True, cwd=ROOT)
    return out.returncode == 0


def is_url(s):
    return s.startswith(("http://", "https://"))


def prompt(url, data, header="Fields extracted from the page"):
    return PROMPT.format(url=url, spec=SPEC.read_text(),
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
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in ROTATE_ON:
                sys.exit(f"gemini {e.code}: {e.read().decode()[:500]}")
            last = f"{e.code} on key {i + 1}"
            print(f"key {i + 1}: {e.code}, trying next", file=sys.stderr)
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
    data = None if re.search(r"(youtube\.com|youtu\.be)/", url) else scrape(url)
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
    print("ok")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "-m"]
    if args[:1] == ["selftest"]:
        selftest()
    elif args[:1] == ["find"]:
        print("\n".join(find(" ".join(args[1:]))))
    elif args[:1] == ["cook"]:
        for u, title in federation(" ".join(args[1:])):
            print(f"{title}\n  {u}")
    elif not args:
        sys.exit(__doc__)
    else:
        sys.stdout.write(recipe(args[0], args[1] if len(args) > 1 else "gemini-3.8-flash"))

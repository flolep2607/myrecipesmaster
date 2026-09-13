#!/usr/bin/env python3
"""Import a recipe into Cooklang with Gemini. Web pages and YouTube videos.

  ./tools/ai_import.py <url> > recipes/dinner/Name.cook
  ./tools/ai_import.py <url> -m gemini-3.8-pro      # default: gemini-3.8-flash
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
import json, os, random, re, sys, urllib.error, urllib.request
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
- Metric units (g, ml, tbsp, tsp), servings always a plain number.
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


def prompt(url, data):
    return PROMPT.format(url=url, spec=SPEC.read_text(),
                         data=f"\nFields extracted from the page:\n{data}\n" if data else "")


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


def generate(url, data, model):
    payload = json.dumps(body(url, data)).encode()
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


def clean(resp):
    parts = resp["candidates"][0].get("content", {}).get("parts", [])
    return "".join(p["text"] for p in parts if "text" in p).strip()


def unfence(text):
    return re.sub(r"\A```[a-z]*\n|\n```\Z", "", text.strip()).strip()


def recipe(url, model):
    """Scraped fields are plain text work for the free endpoint; reading the page
    or the video is Gemini's job, and so is anything the free endpoint drops."""
    data = None if re.search(r"(youtube\.com|youtu\.be)/", url) else scrape(url)
    text = omni(prompt(url, data)) if data else None
    text = text or clean(generate(url, data, model))
    text = unfence(text)
    if not text:
        sys.exit("empty response from both providers")
    return text + "\n"


def selftest():
    assert [k for k in re.split(r"[\s,]+", "a, b\nc") if k] == ["a", "b", "c"]
    assert "file_data" in json.dumps(body("https://youtu.be/x"))
    assert "url_context" in json.dumps(body("https://example.com/r"))
    scraped = json.dumps(body("https://example.com/r", '{"ingredients": ["1 egg"]}'))
    assert "url_context" not in scraped and "1 egg" in scraped
    assert clean({"candidates": [{"content": {"parts": [{"text": " @egg{1} "}]}}]}) == "@egg{1}"
    assert unfence("```cooklang\n@egg{1}\n```") == "@egg{1}"
    print("ok")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "-m"]
    if args[:1] == ["selftest"]:
        selftest()
    elif not args:
        sys.exit(__doc__)
    else:
        sys.stdout.write(recipe(args[0], args[1] if len(args) > 1 else "gemini-3.8-flash"))

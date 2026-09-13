#!/usr/bin/env python3
"""Import a recipe into Cooklang with Gemini. Web pages and YouTube videos.

  ./tools/ai_import.py <url> > recipes/dinner/Name.cook
  ./tools/ai_import.py <url> -m gemini-3.8-pro      # default: gemini-3.8-flash
  ./tools/ai_import.py selftest

Use when `cook import` has no parser for the site, or the source is a video.
Keys: config/gemini.keys, one per line (gitignored), or $GEMINI_API_KEYS
comma-separated. A random key starts each run and quota/server errors fall
through to the next one.
"""
import json, os, random, re, sys, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = ROOT / "config/gemini.keys"
SPEC_CACHE = ROOT / "data/cooklang-extensions.md"
SPEC_URL = "https://raw.githubusercontent.com/cooklang/cooklang-rs/refs/heads/main/extensions.md"
API = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
ROTATE_ON = {429, 403, 500, 503}

PROMPT = """Convert this recipe into a single Cooklang file. Output ONLY the file, no commentary, no code fences.

Format:
---
title: <dish name>
servings: <number>
tags: <comma, separated>
source: {url}
time: <e.g. 45 minutes>
---

Then the steps, one paragraph per step, with ingredients as @name{{qty%unit}},
cookware as #pan{{}} and timers as ~{{10%minutes}}.

Rules:
- Ingredient names lowercase and singular (@egg, @onion), multi-word ones need the braces: @olive oil{{2%tbsp}}.
- Metric units (g, ml, tbsp, tsp), servings always a plain number.
- Tag every ingredient the first time the method uses it; later mentions are references, @&name{{qty%unit}},
  which add to the first amount. Modifiers @?optional, @-hidden and @@other recipe{{}} are available too.
- Keep the method wording of the source; do not invent steps or quantities. Guess a quantity only if the source truly omits it.

Cooklang syntax reference:
{spec}

Source: {url}
"""


def spec():
    if not SPEC_CACHE.exists():
        SPEC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(SPEC_URL, timeout=30) as r:
            SPEC_CACHE.write_bytes(r.read())
    return SPEC_CACHE.read_text()


def keys():
    raw = os.environ.get("GEMINI_API_KEYS") or (KEYS_FILE.read_text() if KEYS_FILE.exists() else "")
    ks = [k for k in re.split(r"[\s,]+", raw) if k and not k.startswith("#")]
    if not ks:
        sys.exit(f"No API keys. Put one per line in {KEYS_FILE} or set $GEMINI_API_KEYS.")
    random.shuffle(ks)
    return ks


def body(url):
    """YouTube goes in as video, anything else is read by the url_context tool."""
    text = PROMPT.format(url=url, spec=spec())
    if re.search(r"(youtube\.com|youtu\.be)/", url):
        return {"contents": [{"parts": [{"file_data": {"file_uri": url}}, {"text": text}]}]}
    return {"contents": [{"parts": [{"text": text}]}], "tools": [{"url_context": {}}]}


def generate(url, model):
    payload = json.dumps(body(url)).encode()
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
    text = "".join(p["text"] for p in parts if "text" in p).strip()
    text = re.sub(r"\A```[a-z]*\n|\n```\Z", "", text).strip()
    if not text:
        sys.exit("empty response: " + json.dumps(resp)[:500])
    return text + "\n"


def selftest():
    assert [k for k in re.split(r"[\s,]+", "a, b\nc") if k] == ["a", "b", "c"]
    assert "file_data" in json.dumps(body("https://youtu.be/x"))
    assert "url_context" in json.dumps(body("https://example.com/r"))
    assert clean({"candidates": [{"content": {"parts": [{"text": "```cooklang\n@egg{1}\n```"}]}}]}) == "@egg{1}\n"
    print("ok")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "-m"]
    if args[:1] == ["selftest"]:
        selftest()
    elif not args:
        sys.exit(__doc__)
    else:
        sys.stdout.write(clean(generate(args[0], args[1] if len(args) > 1 else "gemini-3.8-flash")))

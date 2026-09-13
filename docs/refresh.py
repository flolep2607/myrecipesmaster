#!/usr/bin/env python3
"""Re-download the vendored cooklang docs in this folder. No args."""
import urllib.request
from pathlib import Path

RAW = "https://raw.githubusercontent.com/%s/refs/heads/main/%s"
SOURCES = {
    "spec.md": ("cooklang/spec", "README.md"),
    "spec-ebnf.md": ("cooklang/spec", "EBNF.md"),
    "conventions.md": ("cooklang/spec", "conventions.md"),
    "extensions.md": ("cooklang/cooklang-rs", "extensions.md"),
    "best-practices.md": ("cooklang/cooklang.org", "content/docs/best-practices.md"),
    "cli-commands.md": ("cooklang/cooklang.org", "content/docs/getting-started-commands.md"),
    "for-developers.md": ("cooklang/cooklang.org", "content/docs/for-developers.md"),
    "examples.md": ("cooklang/cooklang.org", "content/docs/examples.md"),
    "proposal-0008-mise-en-place.md": ("cooklang/spec", "proposals/0008-mise-en-place.md"),
    "proposal-0010-servings.md": ("cooklang/spec", "proposals/0010-servings.md"),
    "proposal-0015-other-recipes.md": ("cooklang/spec", "proposals/0015-other-recipes.md"),
    "proposal-0016-shopping-list-format.md": ("cooklang/spec", "proposals/0016-shopping-list-format.md"),
}

for name, (repo, path) in SOURCES.items():
    with urllib.request.urlopen(RAW % (repo, path), timeout=30) as r:
        (Path(__file__).parent / name).write_bytes(r.read())
    print(f"{name}  <- {repo}/{path}")

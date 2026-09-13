# Vendored cooklang docs

Upstream copies, so the parser rules are readable offline and `tools/ai_import.py`
can paste `extensions.md` into its prompt. `./docs/refresh.py` re-downloads them all.

| File | Upstream | What's in it |
|---|---|---|
| `spec.md` | cooklang/spec `README.md` | the language itself: ingredients, cookware, timers, metadata |
| `spec-ebnf.md` | cooklang/spec `EBNF.md` | the grammar, for when a parse surprises you |
| `conventions.md` | cooklang/spec `conventions.md` | canonical metadata keys, `.menu` plans, `.shopping-list` format, recipe scaling |
| `extensions.md` | cooklang/cooklang-rs `extensions.md` | modifiers `@&` `@?` `@-` `@@`, intermediate preparations, ranges, sections — only work on our patched CookCLI |
| `best-practices.md` | cooklang.org `content/docs/best-practices.md` | writing habits that keep a collection tidy |
| `cli-commands.md` | cooklang.org `content/docs/getting-started-commands.md` | every `cook` subcommand with examples |
| `for-developers.md` | cooklang.org `content/docs/for-developers.md` | the parser libraries and bindings |
| `examples.md` | cooklang.org `content/docs/examples.md` | worked recipe examples |

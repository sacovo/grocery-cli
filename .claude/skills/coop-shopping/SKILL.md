---
name: coop-shopping
description: Search products on coop.ch and manage Coop shopping lists via the `coop` CLI in this repo. Use when asked to find Swiss grocery products, check prices or ingredients/allergens, turn a recipe into a shopping list, or add/remove items on a Coop list. Covers dietary filters (vegan, gluten-free, …), category scoping, and JSON output for scripting.
---

# Coop shopping CLI

Wraps coop.ch. Every command drives a real Chrome browser, so treat it as a slow,
stateful, side-effecting tool — not a fast HTTP API.

Run from the repo root: `uv run coop <command>`.

## Check readiness first

```bash
uv run coop status --json
```

```json
{"chrome": true, "logged_in": true, "active_list": {"id": "…", "name": "SoLa Di-Do"}}
```

Do this before any multi-step task. Exit `3` means nobody is logged in — that needs the
**user**, not you: ask them to log in to Supercard in the Chrome window the tool opens.
You cannot log in on their behalf, and there is no headless mode.

## Always pass `--json`

Every command supports it. Human output is a Rich table that will mangle under parsing.
JSON goes to stdout; all messages, warnings and errors go to stderr, so stdout stays
clean for `| jq`.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Success | — |
| 1 | Ran fine, found nothing | Not an error. Try a broader term. |
| 2 | Bad request, or Coop behaved unexpectedly | Read stderr. |
| 3 | Not logged in | Stop and ask the user to log in. |
| 4 | DataDome bot wall | Ask the user to interact with the Chrome window once, then retry. |

Exit `1` is an empty answer, not a failure. Do not retry it unchanged.

## Commands

```bash
uv run coop status  --json                       # readiness
uv run coop search "Milch 3.5%" -n 10 --json     # find products
uv run coop categories Parmesan --json           # category codes for narrowing
uv run coop product 6850514 --json               # ingredients, allergens, nutrition
uv run coop list --json                          # the active shopping list
uv run coop lists --json                         # all lists, active one flagged
uv run coop use "SoLa Di-Do"                     # switch active list
uv run coop new-list "Grillabend"                # create (becomes active)
uv run coop add 6850514 7362485 --json           # add by product id
uv run coop remove 6850514 --json                # remove by product id
uv run coop recipe recipe.txt --json             # resolve ingredients (read-only)
uv run coop recipe recipe.txt --json --add       # …and write them to the list
```

## Exports

For handing data to a spreadsheet or an inventory system. CSV by default,
`--format json` for full fidelity, `-o FILE` to write to disk.

```bash
uv run coop export list                               # active list -> CSV on stdout
uv run coop export list --list "Grillabend" -o l.csv  # a named list
uv run coop export products 6850514 7362485 -o p.csv  # named products
uv run coop export products --from-list -o p.csv      # every product on the list
```

- **List export** columns: `product_id, name, quantity, price, line_total, entry_id`.
- **Product export** carries `ingredients`, `allergens`, `storage`, `origin`, `gtin` and
  friends, plus dynamic `nutrition:*` and `info:*` columns unioned across the products.

Both are read-only — `--list` switches to that list and switches back.

`export products --from-list` is **one page load per product**: a 110-item list takes
several minutes. Prefer explicit ids when the user only needs a few, warn before running
the full-list variant, and set a long timeout.

`add` and `remove` are idempotent: adding twice does not duplicate, and the JSON
separates `added` from `already_present`.

## Narrowing a vague search

Free-text search is loose. "Parmesan" ranks *Knorr Risotto Parmesan* highly because Coop
genuinely does. Two ways to narrow, in order of effectiveness:

1. **Category scoping** — `coop categories Parmesan --json` returns e.g. `m_0080`
   (Reibkäse), then `coop search Parmesan --category m_0080 --json`.
2. **Dietary filters** — `--vegan --vegetarian --lactose-free --gluten-free --bio`.

## Filters vs preferences

This distinction matters and is deliberate:

- **Filters** (`--vegan`, `--gluten-free`, …) are **hard constraints**. If nothing
  matches, the ingredient comes back unresolved rather than falling back. A dietary
  restriction is never silently dropped.
- **`--prefer`** (`bio`, `budget`, `cheapest`) is a **soft bias**. If nothing matches it,
  the normal best match is used and `preference_applied` is `false` in the JSON.

When a user mentions an allergy or a diet, use a **filter**, never `--prefer`.

## Recipes

Input is one ingredient per line; `#` lines are ignored. Amounts and units are parsed off
(`500 g Spaghetti`, `2 Esslöffel Olivenöl`, `½ TL Salz`, `2-3 Zwiebeln`).

```bash
printf '500 g Spaghetti\n4 Zehen Knoblauch\n2 EL Olivenöl\n' \
  | uv run coop recipe --json --prefer budget
```

Key JSON fields per match: `term`, `amount`, `best`, `alternatives`, `resolved`,
`spoon_scale`, `preference_applied`, `note`.

**Spoon-scale ingredients** (`EL`, `TL`, `Prise`, `Msp`) are flagged `spoon_scale: true`.
No pack size satisfies "2 tablespoons of oil", and it usually means a staple the user
already owns. `--skip-pantry` drops them. It keys off the unit, not the ingredient, so it
would also skip `1 Prise Safran` — mention that rather than applying it silently.

Units are **not** converted to grams. A tablespoon of flour and one of oil differ ~2× by
weight. If asked for gram equivalents, say the tool deliberately does not guess.

## Rules

- **Never `--interactive` / `-i`.** It prompts for keyboard input and will hang. It is
  rejected outright when combined with `--json`.
- **Resolve before you write.** Run `coop recipe` without `--add` first, show the user
  the matches, and only then re-run with `--add`. Matching is genuinely fuzzy.
- **Confirm before writing to the user's account.** `add`, `remove`, `new-list`, `use`
  and `recipe --add` all change real state on their Coop account.
- **Never delete lists.** Not supported, deliberately.
- **Ordering is out of scope.** Nothing here places an order; checkout stays manual.

## Latency

Each search is a browser navigation, roughly 5–15 s. A 10-ingredient recipe takes a
couple of minutes, and `--prefer` can double that (it searches twice when the preference
matches nothing). Set generous timeouts, run one command at a time — concurrent
invocations fight over the same browser tab — and tell the user when something will be
slow rather than looking hung.

## Allergens

`coop product <id> --json` returns `allergens`, read from the ingredients Coop prints in
bold. It reproduces Coop's declaration; it is not an independent safety check. For a
serious allergy, surface the full `ingredients` string and say the packaging is the
authority.

# coop-api

A small Python wrapper around [coop.ch](https://www.coop.ch) for automating grocery
shopping: search products, resolve recipe ingredients to real articles, and push them
onto your Coop shopping list.

## How it works, and why

coop.ch sits behind **DataDome** bot protection. Every path — including `robots.txt` —
returns a CAPTCHA interstitial to plain HTTP clients, so `requests`/`httpx` is a dead
end. Playwright's own browser is rejected too: its automation flags set
`navigator.webdriver = true`, which DataDome flags immediately.

So this library does not launch an instrumented browser and does not spoof anything.
It starts an **ordinary Chrome** and attaches to its debug port. That is a genuine
browser running your own logged-in Supercard session — the site gets exactly the
client it expects.

Consequences worth knowing up front:

- **A visible Chrome window is required.** No headless mode.
- **You log in yourself, once.** The tool never handles your credentials. The session
  persists in a dedicated profile at `~/.coop-api/chrome-profile`.
- Search works anonymously; anything touching the shopping list needs the login.
- This is personal-scale automation. Don't point it at bulk scraping.

## Setup

```bash
uv sync
```

First run opens a Chrome window on coop.ch. Log in via Supercard, and you're set —
later runs reuse that profile.

## CLI

```bash
uv run coop search "Milch 3.5%" -n 10   # search products
uv run coop product 6850514             # ingredients, allergens, nutrition
uv run coop list                        # show the active shopping list
uv run coop add 6850514                 # add by product ID
uv run coop remove 6850514              # remove by product ID

uv run coop lists                       # all your lists, active one marked
uv run coop use "SoLa Di-Do"            # switch the active list (name or id)
uv run coop new-list "Grillabend"       # create one (Coop makes it active)

# resolve a recipe's ingredients (read-only by default)
uv run coop recipe recipe.txt
uv run coop recipe recipe.txt --add     # actually write to the list
```

### Categories and dietary filters

Free-text search is vague — "Parmesan" also matches risotto seasoning. Ask which
categories Coop suggests, then scope to one:

```bash
uv run coop categories Parmesan
#  m_0390  Sbrinz, Parmesan & Gruyère
#  m_0080  Reibkäse

uv run coop search Parmesan --category m_0080
```

Dietary flags work on both `search` and `recipe`, and matter most when cooking for
guests: `--vegan`, `--vegetarian`, `--lactose-free`, `--gluten-free`, `--bio`.

```bash
uv run coop search Rahm --vegan         # only plant-based cream alternatives
uv run coop recipe carbonara.txt --vegan
```

### Choosing between candidates

`--prefer` biases which product a recipe resolves to:

```bash
uv run coop recipe carbonara.txt --prefer budget    # Prix Garantie where available
uv run coop recipe carbonara.txt --prefer bio       # Naturaplan and other organics
uv run coop recipe carbonara.txt --prefer cheapest  # lowest price wins
```

`budget` also answers to `"prix garantie"`, and `bio` to `"organic"`.

**Filters and preferences are not the same thing**, and the difference is deliberate:

| | Behaviour when nothing matches |
| --- | --- |
| `--vegan`, `--gluten-free`, … | Hard constraint. The ingredient is reported unresolved. |
| `--prefer …` | Soft bias. Falls back to the normal best match and says so. |

That asymmetry is the point: quietly dropping "vegan" could poison a guest, whereas
quietly dropping "bio" just costs a preference. Fallbacks are always printed:

```
no 'bio' option for 'Alufolie' - used the normal best match
```

`bio` and `budget` use Coop's own facets (`bioFacet`, brand `Prix Garantie`) rather than
matching on product names, so "Biotta" juice is not mistaken for an organic product.

### Interactive mode

`-i` / `--interactive` shows the candidates per ingredient and lets you pick:

```bash
uv run coop recipe carbonara.txt --interactive
```

```
(2/3) 500 g Spaghetti  -> Spaghetti
   1 Barilla Spaghetti No. 5  CHF 2.50 · 500g · 0.50/100g
   2 Prix Garantie Spaghetti  CHF 1.20 · 1000g · 0.12/100g
   3 Rummo Spaghetti No.3  CHF 2.80 · 500g · 0.56/100g
  pick [number / s=skip / q=quit]:
```

`s` skips an ingredient, `q` aborts without writing anything. At the end you get the
basket and a running total, and nothing is written until you confirm — so `--interactive`
does not need `--add`. It combines with `--prefer` and the dietary flags, which change
the order and contents of the candidate list.

`recipe.txt` is one ingredient per line; `#` lines are ignored. Amounts and units are
stripped before searching:

```
# Spaghetti Carbonara
500 g Spaghetti
200 g Speckwürfel
4 Eier
```

## Library

```python
from coop_api import CoopClient, SearchFilters

with CoopClient() as coop:
    hits = coop.search("Milch 3.5%", limit=10)
    for p in hits:
        print(p.id, p.name, p.price, p.unit_price, p.orderable)

    coop.search("Parmesan", category="m_0080")          # scope to a category
    coop.search("Rahm", vegan=True, bio=True)           # inline dietary filters
    coop.search("Pasta", filters=SearchFilters(gluten_free=True))
    coop.suggest_categories("Parmesan")                 # [{'code': 'm_0080', ...}]

    coop.add_to_list(hits[0])          # True if added, False if already there
    items = coop.get_list()            # ListItem(product_id, entry_id, name, quantity, price)
    coop.remove_from_list(hits[0])

    # multiple lists
    coop.get_lists()                   # [ShoppingList(id, name, active), ...]
    coop.select_list("Grillabend")     # add_to_list/get_list now act on this one
    coop.create_list("Weihnachten")

    # recipe resolution with a soft preference
    from coop_api.recipes import resolve
    matches = resolve(coop, ["500 g Spaghetti"], prefer="budget")
    matches[0].best                    # Product
    matches[0].candidates              # best + alternatives, for your own picker
    matches[0].preference_applied      # False if the preference had to be dropped

    # full product data
    d = coop.get_product("6850514")
    d.ingredients                      # 'IP-SUISSE standardisierte WIESENVOLLMILCH (Schweiz).'
    d.allergens                        # ['WIESENVOLLMILCH']
    d.nutrition                        # [('Fett', '3.5g'), ('Kohlenhydrate', '4.9g'), ...]
    d.nutrition_base, d.gtin, d.origin
```

Other entry points: `coop.category("m_0056")` lists a category, `coop.add_many([...])`
adds in bulk, and `coop_api.recipes.resolve()` gives you ranked candidates per
ingredient without writing anything. `coop_api.filters.ALL_PROPERTIES` lists every
nutritional facet Coop exposes (high protein, no added sugar, …), usable via
`SearchFilters(properties=("HIGH_PROTEIN",))`.

Searching for a bare product ID works too — `coop.search("6850514")` returns that
article, which is handy for re-resolving a saved shopping list.

## Notes on behaviour

- **`add_to_list` is idempotent.** Coop's control is a toggle whose label does *not*
  reflect current state, so clicking blindly can remove an item or double-count it.
  The list itself is the source of truth: the client checks before clicking and
  verifies afterwards. Removal is a separate control that only exists on the list page.
- **Recipe matching is a shortlist, not an answer.** Ingredient names are ambiguous and
  Coop's index is the only judge of relevance. `coop recipe` prints alternatives and
  writes nothing unless you pass `--add`. Check the matches before committing —
  especially with `--category`, which is the sharpest tool for narrowing a vague term.
- **Dietary filters are applied by the site, not by us.** We tick the real filter-panel
  checkboxes and confirm the result count moved; we never infer "is this vegan?" from a
  product name. If a filter isn't offered for a query (Coop only lists facets with at
  least one hit), the search raises rather than returning unfiltered results, and
  `coop recipe` reports that ingredient as unresolved. A restriction is never silently
  dropped.
- **Allergens come from Coop's own markup, not from guessing.** Coop bolds the allergenic
  ingredients in its ingredient text; `allergens` is exactly that list. It is a
  reproduction of Coop's declaration, not an independent safety check — for a severe
  allergy, read `ingredients` in full and verify against the physical packaging.
- **Nutrition values are verbatim.** Coop prints two `Energie` rows (kJ then kcal) with
  no unit on either, so both appear as-is rather than being labelled by guesswork.
- **List operations act on the active list.** `select_list` changes which list
  `get_list`/`add_to_list` touch, and Coop makes a newly created list active. There is
  deliberately no delete-list support.
- **Ordering is deliberately out of scope.** Nothing here places an order; checkout
  stays manual.

## Development

Code quality runs through [qlty](https://qlty.sh) (ruff for lint/format, bandit for
security, radarlint for complexity):

```bash
qlty fmt --all      # format
qlty check --all    # lint + security
qlty smells --all   # complexity and duplication
```

Ruff configuration lives in `.ruff.toml` at the repo root rather than in
`pyproject.toml`, so the local CLI, editors and qlty's bundled ruff all read the same
settings. Two `# nosec` suppressions in `session.py` are deliberate and annotated: this
package launches Chrome, so it must import and call `subprocess`.

## Layout

| Module | Responsibility |
| --- | --- |
| `session.py` | Launching/attaching Chrome, DataDome and login detection |
| `extractors.py` | Every JavaScript snippet that reads Coop's markup |
| `client.py` | `CoopClient` — search, products, lists |
| `filters.py` | Facet definitions and the `SearchFilters` selection |
| `recipes.py` | Ingredient-line parsing and candidate ranking |
| `models.py` | `Product`, `ProductDetail`, `ListItem`, `ShoppingList` |
| `cli.py` | Typer commands |

The split that matters is `extractors.py`: all the selector knowledge sits there, so a
Coop redesign should touch one file rather than being spread through the client.

## Fragility

Product data is read from listing markup (`[data-producttile-wrapper]` and friends), so
a Coop redesign will break extraction. Every selector lives in `extractors.py` — that's
the first place to look. `BotWall` is raised if DataDome ever intercepts a navigation;
interacting with the Chrome window once and retrying usually clears it.

Two traps worth remembering if you touch the search code:

- Tile extraction is scoped to `.list-page__content`. A query with no matches still
  renders recommendation carousels (`.productCarouselIsolated__item`) full of unrelated
  products. Widening that selector makes "no vegan bacon" silently return burger sauce.
- Facets **cannot** be applied by URL. The `q=...:nutritionalProperties:VEGAN` form that
  appears in the address bar is only pushState'd by Coop's JavaScript; requesting it
  directly returns unfiltered results with no error. Category pages are the exception —
  there `q=` does filter server-side — but the checkbox path is used everywhere for
  consistency.

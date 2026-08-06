# grocery

Automate Swiss grocery shopping from recipes: search products, resolve recipe
ingredients to real articles, and push them onto your shopping list.

Two stores are supported, selected with `--store` (or `GROCERY_STORE`):

| | coop.ch | migros.ch |
| --- | --- | --- |
| Access | scraped HTML behind DataDome | JSON API |
| Product details | one page load each | batched by uid |
| Dietary filters | filter-panel checkboxes | first-class facets |
| Writes | click, then verify | one PUT that returns the new list |

Both drive the same real Chrome, for different reasons: Coop because DataDome rejects
automated browsers, Migros because its API needs an OAuth token only a signed-in browser
obtains. Neither ever checks out.

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
  persists in a dedicated profile at `~/.grocery/chrome-profile` (an existing
  `~/.coop-api/chrome-profile` keeps being used, so upgrading does not log you out).
- Search works anonymously; anything touching the shopping list needs the login.
- This is personal-scale automation. Don't point it at bulk scraping.

## Setup

```bash
uv sync
```

First run opens a Chrome window on coop.ch. Log in via Supercard, and you're set —
later runs reuse that profile.

## CLI

`--store coop` (the default) or `--store migros` / `-s migros` picks the backend; the
`GROCERY_STORE` environment variable sets it once.

```bash
uv run coop -s migros search Vollmilch -n 5   # same commands, other store
uv run coop -s migros list                    # Migros' list *is* its basket
uv run coop -s migros add 100006357
uv run coop -s migros new-list "Grillabend"     # create
uv run coop -s migros delete-list "Grillabend"  # delete (Migros only, confirms first)

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
parsed off before searching:

```
# Spaghetti Carbonara
500 g Spaghetti
200 g Speckwürfel
4 Eier
```

### Measurements

Quantities are parsed, not just discarded, and shown in an `Amount` column. Handled:
metric weights and volumes, spelled-out and abbreviated German units (`EL`/`Esslöffel`,
`TL`/`Teelöffel`, `Msp`/`Messerspitze`, `Prise`, `Zehe`, `Bund`, `Scheibe`, `Glas`,
`Päckchen`, …) including irregular plurals, plus fractions in every form recipes use
them — `1/2`, `½`, `1½`. Ranges like `2-3 Zwiebeln` resolve to the upper bound, on the
grounds that having too much is the cheaper mistake.

**Spoon- and pinch-scale amounts are treated differently.** Nobody sells two tablespoons
of olive oil, so `2 EL Olivenöl` tells you nothing about which pack to buy — and usually
means a staple already in the cupboard. Those lines are flagged in the output, and
`--skip-pantry` leaves them out:

```bash
uv run coop recipe aglio-e-olio.txt --skip-pantry
# Skipped 3 pantry-scale: Olivenöl, Chiliflocken, Salz
```

The flag keys off the *unit*, not the ingredient, so it is a heuristic: `1 Prise Safran`
gets skipped alongside the salt. It is off by default for that reason.

**Units are deliberately not converted to grams or millilitres.** A tablespoon of flour
and a tablespoon of oil differ by roughly a factor of two by weight, and a Swiss `EL` is
not a US tablespoon. Converting would produce confident numbers that are quietly wrong,
so the amount is reported as written and left for you to judge.

## Library

```python
from grocery import CoopClient, SearchFilters

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
    from grocery.recipes import resolve
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
adds in bulk, and `grocery.recipes.resolve()` gives you ranked candidates per
ingredient without writing anything. `grocery.stores.coop.filters.ALL_PROPERTIES` lists every
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
  `get_list`/`add_to_list` touch, and a newly created list becomes active.
- **Deleting lists is Migros-only**, because only Migros exposes an endpoint for it
  (`DELETE /shopping-list/public/v1/list/delete/{id}`). It is permanent with no undo, so
  `coop -s migros delete-list NAME` confirms first; `--yes` skips that for scripts. Coop
  has no equivalent and says so.
- **Ordering is deliberately out of scope.** Nothing here places an order; checkout
  stays manual.

## Exports

For feeding a spreadsheet or an inventory system. CSV by default, `--format json` for
full fidelity, `-o FILE` to write to disk (progress then goes to stderr).

```bash
uv run coop export list                              # active list, CSV to stdout
uv run coop export list --list "Grillabend" -o l.csv # a specific list
uv run coop export products 6850514 7362485 -o p.csv # named products
uv run coop export products --from-list -o p.csv     # every product on the list
```

**List export** — one row per entry, with a computed line total:

```csv
product_id,name,quantity,price,line_total,entry_id
6139128,Thai Kitchen Kokosnussmilch,13,7.5,97.5,12B36FC4-…
```

**Product export** — the scalar fields (`id`, `name`, `brand`, `price`, `gtin`,
`ingredients`, `allergens`, `origin`, `production_country`, `storage`, `description`,
`url`, `nutrition_base`), then dynamic columns for everything that varies per product:

- `nutrition:Fett`, `nutrition:Kohlenhydrate`, … one per nutrient. Coop prints two rows
  both labelled `Energie` (kJ then kcal) with no unit on either, so the second becomes
  `nutrition:Energie (2)` rather than being silently dropped.
- `info:Lagerung`, `info:Artikelnummer`, `info:ECO-SCORE®`, … whatever extra sections
  the product page carries.

Columns are the union across the exported products, so a field only one product has
still gets a column, empty for the rest.

Both exports are **read-only**. `--list` switches to that list and switches back, so
exporting never leaves a different list active.

Store differences worth knowing before you import the output:

- **Migros list prices are fetched separately.** Its shopping-list API returns no prices,
  so they come from one batched product lookup — without that the `price` and
  `line_total` columns would be empty.
- **Migros ingredients/nutrition are scraped**, not from an API: nothing the site calls
  exposes them. Nutrition there carries real units (`297 kJ (71 kcal)`), unlike Coop's
  unitless `Energie` rows.
- **Single-ingredient products have no ingredient list** at either store — milk exports
  with an empty `ingredients` cell because there genuinely isn't one.
- **`new-list` works on both stores.** Migros creates via
  `POST /shopping-list/public/v1/list`.

`export products` costs one page load per product — a 110-item list takes minutes.
Progress is reported on stderr so stdout stays pipeable, and a product that fails to
load is skipped with a warning rather than aborting the run.

## Scripting and agents

Every command takes `--json`, writing machine-readable output to stdout while all
messages and errors go to stderr — so `coop search … --json | jq` is safe.

```bash
uv run coop status --json     # {"chrome": true, "logged_in": true, "active_list": {…}}
uv run coop search Milch --json | jq -r '.products[].id'
```

Exit codes are meaningful, so callers can branch without parsing messages:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Ran fine, found nothing (an empty answer, not a failure) |
| 2 | Invalid request, or Coop behaved unexpectedly |
| 3 | Not logged in — needs a human at the Chrome window |
| 4 | DataDome bot wall |

`coop status` is the cheap prerequisite check: run it before a long batch rather than
discovering halfway through that nobody is logged in. `add` and `remove` are idempotent,
and their JSON separates `added` from `already_present`.

`--interactive` is rejected when combined with `--json`, since it would block on
keyboard input.

There is a Claude Code skill at `.claude/skills/coop-shopping/SKILL.md` describing this
surface, including the latency characteristics and the filter-versus-preference rule.

## HTTP API

For automation platforms that speak HTTP rather than shelling out (n8n, Home
Assistant, …), the same operations are exposed as a JSON API:

```bash
uv run grocery-api                    # binds 127.0.0.1:8377
uv run grocery-api --host 0.0.0.0 --port 8377
```

It drives the same non-headless Chrome as the CLI: each request opens a client, does
one operation and detaches, and a process-wide lock serialises browser work, so
concurrent requests queue rather than fight over the single page. Interactive docs
(and the OpenAPI schema n8n can import) are at `/docs` and `/openapi.json`.

| Endpoint | Mirrors |
| --- | --- |
| `GET /health` | liveness, never touches Chrome |
| `GET /status` | `coop status` |
| `GET /search?q=…&limit=…&vegan=true…` | `coop search` |
| `GET /categories?q=…` | `coop categories` |
| `GET /products/{id}` | `coop product` |
| `GET /list`, `GET /lists` | `coop list`, `coop lists` |
| `POST /lists/active` `{"name_or_id": …}` | `coop use` |
| `POST /lists` `{"name": …}` | `coop new-list` |
| `DELETE /lists/{name_or_id}` | `coop delete-list` (Migros only, no confirmation) |
| `POST /list/add`, `POST /list/remove` `{"product_ids": […]}` | `coop add` / `remove` |
| `POST /recipe` `{"ingredients": […], "add": false, …}` | `coop recipe` |

Every endpoint takes `?store=coop|migros` (default: `$GROCERY_STORE`, then coop).
Errors map onto status codes the way the CLI maps them onto exit codes: 409 means a
human has to log in at the Chrome window, 503 is the DataDome bot wall, 502 is the
store misbehaving — each with a JSON body like
`{"error": {"code": "not_logged_in", "message": …}}`. "Found nothing" is a 200 with
`count: 0`, not an error.

Set `GROCERY_API_TOKEN` to require `Authorization: Bearer <token>` on every request.
Without it the API is open — fine on loopback, not fine anywhere else, so set it
whenever n8n reaches this over a network (and front it with TLS). In n8n, use an
HTTP Request node with Header Auth; give `/recipe` a generous timeout, since it runs
one product search per ingredient.

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
| `browser.py` | Launching/attaching Chrome; bot-wall detection. Store-agnostic |
| `errors.py` | `GroceryError`, `NotLoggedIn`, `BotWall` |
| `models.py` | `Product`, `ProductDetail`, `ListItem`, `ShoppingList` |
| `recipes.py` | Ingredient-line parsing and candidate ranking |
| `export.py` | CSV/JSON exports |
| `cli.py` | Typer commands and the `--store` switch |
| `api.py` | FastAPI app (`grocery-api`): the same operations over HTTP |
| `stores/base.py` | The `Store` protocol and `Capabilities` |
| `stores/coop/` | `extractors.py` (all Coop selectors), `client.py`, `filters.py` |
| `stores/migros/` | `session.py` (token capture), `client.py` (JSON API) |

Two splits matter. `stores/coop/extractors.py` holds every selector, so a Coop redesign
touches one file. And `Capabilities` lets a store declare what it genuinely supports —
anything it cannot do is refused with a message rather than emulated, which is why
`--vegan` on a store without real dietary facets would error instead of guessing.

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

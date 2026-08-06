"""HTTP API over the store clients, for automation platforms like n8n.

Same operations as the CLI, exposed as JSON endpoints. Each request opens a client,
does one operation and detaches, exactly like a CLI invocation - the API keeps no
browser state between requests. Chrome offers only a single page to drive, so a
process-wide lock serialises all browser work: concurrent requests queue instead of
interleaving navigations and corrupting each other.

Errors map onto status codes the way the CLI maps them onto exit codes:

* 401 - the API token (``GROCERY_API_TOKEN``) is missing or wrong.
* 409 - the store needs a human to log in via the Chrome window (``not_logged_in``).
* 503 - the store served its bot-protection page (``blocked``).
* 502 - the store behaved unexpectedly (``store_error``).

"Found nothing" is not an error over HTTP: searches return 200 with ``count: 0``.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import threading

import typer
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import BotWall, GroceryError, NotLoggedIn
from .recipes import parse_recipe, resolve
from .stores.coop.filters import SearchFilters, normalise_preference

STORES = ("coop", "migros")

# One Chrome, one page: never let two requests drive it at once.
_BROWSER_LOCK = threading.Lock()


def _make_client(store: str):
    if store == "migros":
        from .stores.migros import MigrosClient

        return MigrosClient()
    from .stores.coop import CoopClient

    return CoopClient()


@contextlib.contextmanager
def _client(store: str):
    """A started client with exclusive ownership of the browser."""
    with _BROWSER_LOCK, _make_client(store) as c:
        yield c


def _check_token(authorization: str | None = Header(None)) -> None:
    """Require ``Authorization: Bearer <GROCERY_API_TOKEN>`` when a token is set.

    Without the env var the API is open - fine on localhost, not fine on a network,
    so set the token whenever n8n reaches this over anything but loopback.
    """
    token = os.environ.get("GROCERY_API_TOKEN")
    if not token:
        return
    expected = f"Bearer {token}"
    if not (authorization and secrets.compare_digest(authorization, expected)):
        raise HTTPException(401, "Missing or invalid bearer token.")


StoreQuery = Query(None, description="coop or migros. Defaults to $GROCERY_STORE, then coop.")


def _store(store: str | None = StoreQuery) -> str:
    resolved = store or os.environ.get("GROCERY_STORE") or "coop"
    if resolved not in STORES:
        raise HTTPException(400, f"Unknown store {resolved!r}. Known: {', '.join(STORES)}.")
    return resolved


def _require(store: str, capability: str, what: str) -> None:
    """Refuse a request the configured store cannot honour, rather than faking it."""
    if not getattr(_make_client(store).capabilities, capability, False):
        raise HTTPException(400, f"{store} does not support {what}.")


app = FastAPI(
    title="grocery",
    description="Search Swiss grocers and manage shopping lists via a real Chrome.",
    dependencies=[Depends(_check_token)],
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status)


@app.exception_handler(NotLoggedIn)
def _not_logged_in(request: Request, exc: NotLoggedIn) -> JSONResponse:
    return _error(409, "not_logged_in", str(exc))


@app.exception_handler(BotWall)
def _blocked(request: Request, exc: BotWall) -> JSONResponse:
    return _error(503, "blocked", str(exc))


@app.exception_handler(GroceryError)
def _store_error(request: Request, exc: GroceryError) -> JSONResponse:
    return _error(502, "store_error", str(exc))


# -- reading --------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Liveness only - answers without touching Chrome."""
    return {"ok": True}


@app.get("/status")
def status(store: str = Depends(_store)) -> dict:
    """Readiness: Chrome up, logged in, which list is active.

    Mirrors `coop status` - call it before a long batch rather than discovering
    halfway through that nobody is logged in.
    """
    info: dict[str, object] = {
        "store": store,
        "chrome": False,
        "logged_in": False,
        "active_list": None,
    }
    try:
        with _client(store) as c:
            info["chrome"] = True
            # Entering a Migros client already authenticates; Coop needs a page first.
            if hasattr(c.session, "is_logged_in"):
                c.session.goto(f"https://www.{store}.ch/de/")
                info["logged_in"] = c.session.is_logged_in()
            else:
                info["logged_in"] = bool(c.session.bearer)
            if info["logged_in"]:
                active = next((lst for lst in c.get_lists() if lst.active), None)
                info["active_list"] = active.as_dict() if active else None
    except GroceryError as exc:
        # NotLoggedIn subclasses GroceryError, so this covers both. Status reports
        # rather than fails: the whole point is telling the caller what is wrong.
        info["error"] = str(exc)
    return info


def _filters(
    vegan: bool, vegetarian: bool, lactose_free: bool, gluten_free: bool, bio: bool
) -> SearchFilters:
    return SearchFilters(
        vegan=vegan,
        vegetarian=vegetarian,
        lactose_free=lactose_free,
        gluten_free=gluten_free,
        bio=bio,
    )


@app.get("/search")
def search(
    q: str,
    limit: int = Query(15, ge=1, le=100),
    category: str | None = None,
    vegan: bool = False,
    vegetarian: bool = False,
    lactose_free: bool = False,
    gluten_free: bool = False,
    bio: bool = False,
    store: str = Depends(_store),
) -> dict:
    """Search products by free text, optionally filtered."""
    kwargs = _filters(vegan, vegetarian, lactose_free, gluten_free, bio).as_kwargs()
    if category:
        _require(store, "categories", "category scoping")
        kwargs["category"] = category
    with _client(store) as c:
        hits = c.search(q, limit=limit, **kwargs)
    return {"query": q, "count": len(hits), "products": [p.as_dict() for p in hits]}


@app.get("/categories")
def categories(q: str, store: str = Depends(_store)) -> dict:
    """Category codes the store suggests for a search term."""
    _require(store, "categories", "category suggestions")
    with _client(store) as c:
        found = c.suggest_categories(q)
    return {"query": q, "categories": found}


@app.get("/products/{product_id}")
def product(product_id: str, store: str = Depends(_store)) -> dict:
    """A product's details: ingredients, allergens, nutrition."""
    with _client(store) as c:
        return c.get_product(product_id).as_dict()


# -- shopping lists -------------------------------------------------------------


@app.get("/list")
def show_list(store: str = Depends(_store)) -> dict:
    """The active shopping list's contents."""
    with _client(store) as c:
        items = c.get_list()
    return {"count": len(items), "items": [i.as_dict() for i in items]}


@app.get("/lists")
def lists(store: str = Depends(_store)) -> dict:
    """All shopping lists; the active one is marked."""
    with _client(store) as c:
        found = c.get_lists()
    return {"count": len(found), "lists": [lst.as_dict() for lst in found]}


class ListRef(BaseModel):
    name_or_id: str


@app.post("/lists/active")
def use(body: ListRef, store: str = Depends(_store)) -> dict:
    """Make a shopping list the active one (matched by name or id)."""
    with _client(store) as c:
        selected = c.select_list(body.name_or_id)
        count = len(c.get_list())
    return {**selected.as_dict(), "item_count": count}


class NewList(BaseModel):
    name: str


@app.post("/lists", status_code=201)
def create_list(body: NewList, store: str = Depends(_store)) -> dict:
    """Create a new shopping list. It becomes the active one."""
    _require(store, "multiple_lists", "creating lists")
    with _client(store) as c:
        return c.create_list(body.name).as_dict()


@app.delete("/lists/{name_or_id}")
def delete_list(name_or_id: str, store: str = Depends(_store)) -> dict:
    """Delete a shopping list. Permanent - there is no undo, and no confirmation
    prompt either: over HTTP the DELETE method *is* the confirmation."""
    if store != "migros":
        raise HTTPException(400, f"{store} has no delete endpoint; remove the list in its app.")
    with _client(store) as c:
        target = next(
            (
                lst
                for lst in c.get_lists()
                if lst.id == name_or_id or lst.name.casefold() == name_or_id.casefold()
            ),
            None,
        )
        if target is None:
            raise HTTPException(404, f"No list matching {name_or_id!r}.")
        return {"deleted": c.delete_list(target.id).as_dict()}


class ProductIds(BaseModel):
    product_ids: list[str] = Field(min_length=1)


@app.post("/list/add")
def add(body: ProductIds, store: str = Depends(_store)) -> dict:
    """Add products to the active list by ID. Idempotent."""
    with _client(store) as c:
        results = {pid: c.add_to_list(pid) for pid in body.product_ids}
    return {
        "added": [p for p, new in results.items() if new],
        "already_present": [p for p, new in results.items() if not new],
    }


@app.post("/list/remove")
def remove(body: ProductIds, store: str = Depends(_store)) -> dict:
    """Remove products from the active list by ID. Idempotent."""
    with _client(store) as c:
        results = {pid: c.remove_from_list(pid) for pid in body.product_ids}
    return {
        "removed": [p for p, gone in results.items() if gone],
        "not_on_list": [p for p, gone in results.items() if not gone],
    }


# -- recipes --------------------------------------------------------------------


class RecipeRequest(BaseModel):
    """A recipe to resolve. Give ``ingredients`` as lines, or the whole ``text``."""

    ingredients: list[str] | None = None
    text: str | None = None
    add: bool = False
    """Write the best matches to the shopping list."""
    candidates: int = Field(3, ge=1, le=10)
    prefer: str | None = None
    """"bio", "budget" (Prix Garantie) or "cheapest". Soft - dropped when nothing
    matches it."""
    skip_pantry: bool = False
    """Leave out spoon- and pinch-scale ingredients (EL, TL, Prise)."""
    vegan: bool = False
    vegetarian: bool = False
    lactose_free: bool = False
    gluten_free: bool = False
    bio: bool = False


@app.post("/recipe")
def recipe(body: RecipeRequest, store: str = Depends(_store)) -> dict:
    """Resolve recipe ingredients to products; optionally add them to the list.

    One product search per ingredient, so long recipes take a while - give the HTTP
    client a generous timeout.
    """
    text = body.text if body.text is not None else "\n".join(body.ingredients or [])
    lines = parse_recipe(text)
    if not lines:
        raise HTTPException(400, "No ingredients found.")
    try:
        prefer = normalise_preference(body.prefer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    filters = _filters(body.vegan, body.vegetarian, body.lactose_free, body.gluten_free, body.bio)

    with _client(store) as c:
        matches = resolve(c, lines, candidates=body.candidates + 1, filters=filters, prefer=prefer)
        if body.skip_pantry:
            skipped = [m for m in matches if m.is_spoon_scale]
            matches = [m for m in matches if not m.is_spoon_scale]
        else:
            skipped = []
        written = c.add_many([m.best for m in matches if m.best]) if body.add else {}
    return {
        "prefer": prefer,
        "added_to_list": body.add,
        "matches": [m.as_dict() for m in matches],
        "skipped_pantry": [m.as_dict() for m in skipped],
        "unresolved": [m.term for m in matches if not m.resolved],
        "added": [pid for pid, new in written.items() if new],
        "already_present": [pid for pid, new in written.items() if not new],
    }


# -- entry point ----------------------------------------------------------------


def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8377, help="Port to listen on."),
) -> None:
    """Run the grocery HTTP API.

    Binds to loopback by default. To expose it further, set GROCERY_API_TOKEN and
    front it with something that does TLS.
    """
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    typer.run(serve)


if __name__ == "__main__":
    main()

"""Command line interface: `coop search`, `coop list`, `coop add`, `coop recipe`."""

from __future__ import annotations

import json
import pathlib
import sys

import typer
from rich.console import Console
from rich.table import Table

from .errors import BotWall, GroceryError, NotLoggedIn
from .export import list_rows, product_rows, write_csv
from .models import Product
from .recipes import Match, parse_recipe, resolve
from .stores.coop.client import CoopClient
from .stores.coop.filters import SearchFilters, normalise_preference

app = typer.Typer(
    help="Search Swiss grocers and manage your shopping list.", no_args_is_help=True
)

# Human output on stdout, diagnostics on stderr, so `--json` stdout stays parseable.
console = Console()
err = Console(stderr=True)


class Exit:
    """Exit codes, so a caller can branch without parsing messages."""

    OK = 0
    NO_RESULTS = 1
    """Ran fine, found nothing. Not an error - just an empty answer."""
    ERROR = 2
    """Coop behaved unexpectedly, or the request was invalid."""
    NOT_LOGGED_IN = 3
    """Needs a human to log in to Supercard in the Chrome window."""
    BLOCKED = 4
    """DataDome served its bot-protection page."""


JsonOpt = typer.Option(False, "--json", help="Emit JSON on stdout instead of a table.")


# Which backend the commands talk to. Set once by the global --store option so every
# command does not have to grow its own copy.
_STORE = "coop"

STORES = {
    "coop": "grocery.stores.coop:CoopClient",
    "migros": "grocery.stores.migros:MigrosClient",
}


@app.callback()
def _configure(
    store: str = typer.Option(
        "coop", "--store", "-s", envvar="GROCERY_STORE", help="coop or migros."
    ),
) -> None:
    """Search Swiss grocers and manage your shopping list."""
    global _STORE
    if store not in STORES:
        err.print(f"[red]Unknown store {store!r}. Known: {', '.join(STORES)}.[/]")
        raise typer.Exit(Exit.ERROR)
    _STORE = store


def client():
    """Instantiate the configured store's client."""
    if _STORE == "migros":
        from .stores.migros import MigrosClient

        return MigrosClient()
    from .stores.coop import CoopClient

    return CoopClient()


def _require(capability: str, what: str) -> None:
    """Refuse a command the configured store cannot honour, rather than faking it."""
    if _STORE == "migros":
        from .stores.migros import MigrosClient as _C
    else:
        from .stores.coop import CoopClient as _C
    if not getattr(_C.capabilities, capability, False):
        err.print(f"[red]{_STORE} does not support {what}.[/]")
        raise typer.Exit(Exit.ERROR)


def _emit(payload: object) -> None:
    """Write JSON to stdout, unadorned so it can be piped straight into a parser."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


# Shared dietary options, so `search` and `recipe` behave identically.
VeganOpt = typer.Option(False, "--vegan", help="Only vegan products.")
VegetarianOpt = typer.Option(False, "--vegetarian", help="Only vegetarian products.")
LactoseOpt = typer.Option(False, "--lactose-free", help="Only lactose-free products.")
GlutenOpt = typer.Option(False, "--gluten-free", help="Only gluten-free products.")
BioOpt = typer.Option(False, "--bio", help="Only organic products.")
CategoryOpt = typer.Option(
    None, "--category", help="Scope to a category code, e.g. m_0056."
)


def _products_table(products, title: str) -> Table:
    table = Table(title=title, header_style="bold")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Product")
    table.add_column("Size", no_wrap=True)
    table.add_column("Price", justify="right", no_wrap=True)
    table.add_column("Unit", no_wrap=True)
    table.add_column("Order", justify="center", no_wrap=True)
    for p in products:
        table.add_row(
            p.id,
            p.name,
            p.quantity or "-",
            f"{p.price:.2f}" if p.price is not None else "-",
            p.unit_price or "-",
            "yes" if p.orderable else "no",
        )
    return table


@app.command()
def search(
    query: str,
    limit: int = typer.Option(15, "--limit", "-n", help="Maximum results."),
    category: str = CategoryOpt,
    vegan: bool = VeganOpt,
    vegetarian: bool = VegetarianOpt,
    lactose_free: bool = LactoseOpt,
    gluten_free: bool = GlutenOpt,
    bio: bool = BioOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Search products by free text, optionally filtered."""
    filters = _filters(vegan, vegetarian, lactose_free, gluten_free, bio)
    kwargs = filters.as_kwargs()
    if category:
        _require("categories", "--category")
        kwargs["category"] = category
    with client() as coop:
        hits = coop.search(query, limit=limit, **kwargs)
    if as_json:
        _emit(
            {
                "query": query,
                "count": len(hits),
                "products": [p.as_dict() for p in hits],
            }
        )
        raise typer.Exit(Exit.OK if hits else Exit.NO_RESULTS)
    if not hits:
        err.print(f"[yellow]No products found for {query!r} with those filters.[/]")
        raise typer.Exit(Exit.NO_RESULTS)
    label = ", ".join(filter(None, [category, _filter_label(filters)]))
    title = f"{len(hits)} results for {query!r}" + (f" [{label}]" if label else "")
    console.print(_products_table(hits, title))


@app.command()
def categories(query: str, as_json: bool = JsonOpt) -> None:
    """Show category codes the store suggests for a search term."""
    _require("categories", "category suggestions")
    if _STORE != "coop":
        err.print("[red]Category suggestions are a coop feature.[/]")
        raise typer.Exit(Exit.ERROR)
    with client() as coop:
        found = coop.suggest_categories(query)
    if as_json:
        _emit({"query": query, "categories": found})
        raise typer.Exit(Exit.OK if found else Exit.NO_RESULTS)
    if not found:
        err.print(f"[yellow]No categories suggested for {query!r}.[/]")
        raise typer.Exit(Exit.NO_RESULTS)
    table = Table(title=f"Categories for {query!r}", header_style="bold")
    table.add_column("Code", style="dim", no_wrap=True)
    table.add_column("Name")
    for c in found:
        table.add_row(c["code"], c["name"])
    console.print(table)


@app.command()
def status(as_json: bool = JsonOpt) -> None:
    """Report whether the tool is ready: Chrome up, logged in, which list is active.

    Cheap prerequisite check - run this before a long batch rather than discovering
    halfway through that nobody is logged in.
    """
    info = _gather_status()
    if as_json:
        _emit(info)
    else:
        _render_status(info)

    if not info["chrome"]:
        raise typer.Exit(Exit.ERROR)
    if not info["logged_in"]:
        raise typer.Exit(Exit.NOT_LOGGED_IN)


def _gather_status() -> dict[str, object]:
    info: dict[str, object] = {
        "store": _STORE,
        "chrome": False,
        "logged_in": False,
        "active_list": None,
    }
    try:
        with client() as coop:
            info["chrome"] = True
            # Entering a Migros client already authenticates; Coop needs a page first.
            if hasattr(coop.session, "is_logged_in"):
                coop.session.goto(f"https://www.{_STORE}.ch/de/")
                info["logged_in"] = coop.session.is_logged_in()
            else:
                info["logged_in"] = bool(coop.session.bearer)
            if info["logged_in"]:
                active = next((lst for lst in coop.get_lists() if lst.active), None)
                info["active_list"] = active.as_dict() if active else None
    except GroceryError as exc:
        # NotLoggedIn subclasses GroceryError, so this covers both.
        info["error"] = str(exc)
    return info


def _render_status(info: dict[str, object]) -> None:
    console.print(f"Store:       {info.get('store', '?')}")
    console.print(f"Chrome:      {'up' if info['chrome'] else 'not reachable'}")
    console.print(f"Logged in:   {'yes' if info['logged_in'] else 'no'}")
    active_list = info["active_list"]
    name = active_list["name"] if isinstance(active_list, dict) else "-"
    console.print(f"Active list: {name}")
    if info.get("error"):
        err.print(f"[red]{info['error']}[/]")


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


def _filter_label(filters: SearchFilters) -> str:
    active = [
        name
        for name in ("vegan", "vegetarian", "lactose_free", "gluten_free", "bio")
        if getattr(filters, name)
    ]
    return ", ".join(n.replace("_", "-") for n in active)


@app.command("list")
def show_list(as_json: bool = JsonOpt) -> None:
    """Show the active Coop shopping list."""
    with client() as coop:
        items = coop.get_list()
    if as_json:
        _emit({"count": len(items), "items": [i.as_dict() for i in items]})
        return
    if not items:
        console.print("[yellow]Shopping list is empty.[/]")
        return
    table = Table(title=f"Shopping list ({len(items)} items)", header_style="bold")
    table.add_column("Qty", justify="right", no_wrap=True)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Product")
    table.add_column("Price", justify="right", no_wrap=True)
    for i in items:
        table.add_row(
            str(i.quantity),
            i.product_id,
            i.name,
            f"{i.price:.2f}" if i.price is not None else "-",
        )
    console.print(table)


@app.command()
def lists(as_json: bool = JsonOpt) -> None:
    """Show all shopping lists; the active one is marked."""
    with client() as coop:
        found = coop.get_lists()
    if as_json:
        _emit({"count": len(found), "lists": [lst.as_dict() for lst in found]})
        return
    table = Table(title=f"Shopping lists ({len(found)})", header_style="bold")
    table.add_column("", no_wrap=True)
    table.add_column("Name")
    table.add_column("ID", style="dim", no_wrap=True)
    for lst in found:
        table.add_row("[green]*[/]" if lst.active else "", lst.name, lst.id)
    console.print(table)
    console.print("[dim]* = active. Switch with `coop use <name>`.[/]")


@app.command()
def use(name_or_id: str, as_json: bool = JsonOpt) -> None:
    """Make a shopping list the active one (matched by name or id)."""
    with client() as coop:
        selected = coop.select_list(name_or_id)
        count = len(coop.get_list())
    if as_json:
        _emit({**selected.as_dict(), "item_count": count})
        return
    console.print(f"Active list is now [bold]{selected.name}[/] ({count} items).")


@app.command("new-list")
def new_list(name: str, as_json: bool = JsonOpt) -> None:
    """Create a new shopping list. It becomes the active one."""
    _require("multiple_lists", "creating lists")
    with client() as coop:
        created = coop.create_list(name)
    if as_json:
        _emit(created.as_dict())
        return
    console.print(
        f"Created [bold]{created.name}[/] ({created.id}) — it is now the active list."
    )


@app.command("delete-list")
def delete_list(
    name_or_id: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    as_json: bool = JsonOpt,
) -> None:
    """Delete a shopping list. Permanent - Migros offers no undo.

    Coop has no delete endpoint, so this is Migros-only.
    """
    if _STORE != "migros":
        err.print(
            f"[red]{_STORE} has no delete endpoint; remove the list in its app.[/]"
        )
        raise typer.Exit(Exit.ERROR)

    with client() as store:
        target = next(
            (
                lst
                for lst in store.get_lists()
                if lst.id == name_or_id or lst.name.casefold() == name_or_id.casefold()
            ),
            None,
        )
        if target is None:
            err.print(f"[red]No list matching {name_or_id!r}.[/]")
            raise typer.Exit(Exit.NO_RESULTS)
        count = len(store.get_list()) if target.active else None
        detail = f" ({count} items)" if count is not None else ""
        if not yes and not typer.confirm(
            f"Permanently delete {target.name!r}{detail}? This cannot be undone",
            default=False,
        ):
            err.print("[yellow]Nothing deleted.[/]")
            raise typer.Exit(Exit.OK)
        deleted = store.delete_list(target.id)

    if as_json:
        _emit({"deleted": deleted.as_dict()})
        return
    console.print(f"Deleted [bold]{deleted.name}[/].")


@app.command()
def product(product_id: str, as_json: bool = JsonOpt) -> None:
    """Show a product's details: ingredients, allergens, nutrition."""
    with client() as coop:
        d = coop.get_product(product_id)

    if as_json:
        _emit(d.as_dict())
        return

    console.print(
        f"\n[bold]{d.name}[/]" + (f"  [dim]({d.brand})[/]" if d.brand else "")
    )
    price = f"CHF {d.price:.2f}" if d.price is not None else "n/a"
    console.print(
        f"[dim]{d.id} · {price}" + (f" · EAN {d.gtin}" if d.gtin else "") + "[/]\n"
    )

    if d.ingredients:
        console.print("[bold]Zutaten[/]")
        console.print(f"  {d.ingredients}")
    if d.allergens:
        console.print("\n[bold]Allergens[/] [dim](highlighted by Coop)[/]")
        console.print("  " + ", ".join(d.allergens))
    for label, value in (
        ("Herkunft", d.origin),
        ("Produktionsland", d.production_country),
        ("Lagerung", d.storage),
    ):
        if value:
            console.print(f"\n[bold]{label}[/]\n  {value}")

    if d.nutrition:
        table = Table(
            title=f"Nährwerte pro {d.nutrition_base or '?'}",
            header_style="bold",
            title_justify="left",
        )
        table.add_column("")
        table.add_column("", justify="right")
        for label, value in d.nutrition:
            table.add_row(label, value)
        console.print()
        console.print(table)

    if d.description:
        console.print(f"\n[dim]{d.description[:400]}[/]")


@app.command()
def add(product_ids: list[str], as_json: bool = JsonOpt) -> None:
    """Add products to the shopping list by ID. Idempotent."""
    with client() as coop:
        results = {pid: coop.add_to_list(pid) for pid in product_ids}
    if as_json:
        _emit(
            {
                "added": [p for p, new in results.items() if new],
                "already_present": [p for p, new in results.items() if not new],
            }
        )
        return
    for pid, new in results.items():
        console.print(f"  {pid}: {'added' if new else 'already on list'}")


@app.command()
def remove(product_ids: list[str], as_json: bool = JsonOpt) -> None:
    """Remove products from the shopping list by ID. Idempotent."""
    with client() as coop:
        results = {pid: coop.remove_from_list(pid) for pid in product_ids}
    if as_json:
        _emit(
            {
                "removed": [p for p, gone in results.items() if gone],
                "not_on_list": [p for p, gone in results.items() if not gone],
            }
        )
        return
    for pid, gone in results.items():
        console.print(f"  {pid}: {'removed' if gone else 'was not on list'}")


export_app = typer.Typer(
    help="Export data for other systems (inventory, spreadsheets).",
    no_args_is_help=True,
)
app.add_typer(export_app, name="export")

FormatOpt = typer.Option("csv", "--format", "-f", help="csv or json.")
OutputOpt = typer.Option(
    None, "--output", "-o", help="Write to a file instead of stdout."
)


def _write_export(
    columns: list[str],
    rows: list[dict],
    fmt: str,
    output: pathlib.Path | None,
) -> None:
    """Render rows as CSV or JSON, to a file or stdout."""
    fmt = fmt.lower()
    if fmt not in ("csv", "json"):
        err.print(f"[red]Unknown format {fmt!r}. Use csv or json.[/]")
        raise typer.Exit(Exit.ERROR)

    if output:
        with output.open("w", encoding="utf-8", newline="") as stream:
            if fmt == "csv":
                write_csv(columns, rows, stream)
            else:
                json.dump(rows, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        err.print(f"[green]Wrote {len(rows)} rows to {output}[/]")
        return

    if fmt == "csv":
        write_csv(columns, rows, sys.stdout)
    else:
        _emit(rows)


@export_app.command("list")
def export_list(
    name_or_id: str = typer.Option(
        None, "--list", help="Which list to export. Defaults to the active one."
    ),
    fmt: str = FormatOpt,
    output: pathlib.Path = OutputOpt,
) -> None:
    """Export a shopping list with quantities, prices and line totals.

    Read-only: exporting a non-active list switches to it and switches back.
    """
    with client() as coop:
        if name_or_id:
            if not hasattr(coop, "using_list"):
                coop.select_list(name_or_id)
                items = coop.get_list()
            else:
                with coop.using_list(name_or_id):
                    items = coop.get_list()
        else:
            items = coop.get_list()
    columns, rows = list_rows(items)
    _write_export(columns, rows, fmt, output)


@export_app.command("products")
def export_products(
    product_ids: list[str] = typer.Argument(
        None, help="Product ids. Omit and use --from-list to take them from a list."
    ),
    from_list: bool = typer.Option(
        False, "--from-list", help="Export every product on the shopping list."
    ),
    name_or_id: str = typer.Option(
        None, "--list", help="With --from-list: which list. Defaults to the active one."
    ),
    fmt: str = FormatOpt,
    output: pathlib.Path = OutputOpt,
) -> None:
    """Export full product details: ingredients, allergens, storage, nutrition.

    One page load per product, so exporting a long list takes minutes. Progress is
    reported on stderr, leaving stdout clean to pipe.
    """
    if not product_ids and not from_list:
        err.print("[red]Give product ids, or --from-list to take them from a list.[/]")
        raise typer.Exit(Exit.ERROR)

    with client() as coop:
        if from_list:
            with coop.using_list(name_or_id):
                product_ids = [item.product_id for item in coop.get_list()]
            err.print(f"[dim]Fetching details for {len(product_ids)} products…[/]")

        details = []
        for index, pid in enumerate(product_ids, start=1):
            try:
                details.append(coop.get_product(pid))
            except GroceryError as exc:
                err.print(f"[yellow]  {pid}: skipped ({exc})[/]")
                continue
            if index % 10 == 0 or index == len(product_ids):
                err.print(f"[dim]  {index}/{len(product_ids)}[/]")

    if not details:
        err.print("[yellow]Nothing exported.[/]")
        raise typer.Exit(Exit.NO_RESULTS)
    columns, rows = product_rows(details)
    _write_export(columns, rows, fmt, output)


@app.command()
def recipe(
    path: pathlib.Path = typer.Argument(
        None, help="File with one ingredient per line. Omit to read stdin."
    ),
    add_to_list: bool = typer.Option(
        False, "--add", help="Actually add the matches to the shopping list."
    ),
    candidates: int = typer.Option(
        3, "--candidates", "-c", help="Alternatives to show."
    ),
    prefer: str = typer.Option(
        None,
        "--prefer",
        help='Bias picks: "bio", "budget" (Prix Garantie) or "cheapest". '
        "A preference is soft - it is dropped when nothing matches it.",
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i", help="Choose each product yourself from a list."
    ),
    skip_pantry: bool = typer.Option(
        False,
        "--skip-pantry",
        help="Leave out spoon- and pinch-scale ingredients (EL, TL, Prise) — "
        "the staples you probably already have.",
    ),
    as_json: bool = JsonOpt,
    vegan: bool = VeganOpt,
    vegetarian: bool = VegetarianOpt,
    lactose_free: bool = LactoseOpt,
    gluten_free: bool = GlutenOpt,
    bio: bool = BioOpt,
) -> None:
    """Resolve recipe ingredients to Coop products.

    Prints matches only; pass --add to write them to the shopping list, or --interactive
    to pick each product yourself and confirm before anything is written.

    Dietary flags are hard constraints applied to every ingredient - the point when
    cooking for guests. --prefer is only a bias between otherwise acceptable products.
    """
    if interactive and as_json:
        err.print(
            "[red]--interactive needs a terminal; it cannot be combined with --json.[/]"
        )
        raise typer.Exit(Exit.ERROR)

    lines = _read_recipe(path)
    prefer = _checked_preference(prefer)
    filters = _filters(vegan, vegetarian, lactose_free, gluten_free, bio)
    if not as_json:
        _print_banner(filters, prefer)

    with client() as coop:
        # Interactive mode shows a menu, so it needs more than the usual few.
        wanted = max(candidates + 1, 8) if interactive else candidates + 1
        matches = resolve(
            coop, lines, candidates=wanted, filters=filters, prefer=prefer
        )
        matches, skipped = _split_pantry(matches, skip_pantry, quiet=as_json)

        if as_json:
            _emit_recipe_json(coop, matches, skipped, prefer, add_to_list)
            raise typer.Exit(
                Exit.OK if any(m.resolved for m in matches) else Exit.NO_RESULTS
            )

        if not matches:
            console.print("[yellow]Nothing left after skipping pantry items.[/]")
            return

        if interactive:
            _run_interactive(coop, matches)
            return

        _render_matches(matches, filtered=bool(filters))
        _render_notes(matches, candidates)

        if not add_to_list:
            console.print(
                "\n[dim]Nothing was added. Re-run with --add to write to your list.[/]"
            )
            return
        _apply_matches(coop, matches)


def _read_recipe(path: pathlib.Path | None) -> list[str]:
    text = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    lines = parse_recipe(text)
    if not lines:
        err.print("[yellow]No ingredients found.[/]")
        raise typer.Exit(Exit.NO_RESULTS)
    return lines


def _checked_preference(prefer: str | None) -> str | None:
    try:
        return normalise_preference(prefer)
    except ValueError as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(Exit.ERROR) from exc


def _print_banner(filters: SearchFilters, prefer: str | None) -> None:
    banner = ", ".join(
        filter(None, [_filter_label(filters), f"prefer {prefer}" if prefer else ""])
    )
    if banner:
        console.print(f"[dim]{banner}[/]\n")


def _split_pantry(
    matches: list[Match], skip_pantry: bool, *, quiet: bool
) -> tuple[list[Match], list[Match]]:
    """Partition off spoon-scale ingredients when the caller asked to skip them."""
    if not skip_pantry:
        return matches, []
    skipped = [m for m in matches if m.is_spoon_scale]
    kept = [m for m in matches if not m.is_spoon_scale]
    if skipped and not quiet:
        names = ", ".join(m.term for m in skipped)
        console.print(f"[dim]Skipped {len(skipped)} pantry-scale: {names}[/]\n")
    return kept, skipped


def _emit_recipe_json(
    coop: CoopClient,
    matches: list[Match],
    skipped: list[Match],
    prefer: str | None,
    add_to_list: bool,
) -> None:
    """Emit the resolution result, writing to the list first when asked to."""
    written = coop.add_many([m.best for m in matches if m.best]) if add_to_list else {}
    _emit(
        {
            "prefer": prefer,
            "added_to_list": add_to_list,
            "matches": [m.as_dict() for m in matches],
            "skipped_pantry": [m.as_dict() for m in skipped],
            "unresolved": [m.term for m in matches if not m.resolved],
            "added": [pid for pid, new in written.items() if new],
            "already_present": [pid for pid, new in written.items() if not new],
        }
    )


def _run_interactive(coop: CoopClient, matches: list[Match]) -> None:
    """Walk the ingredients one at a time, letting the user pick, then confirm once."""
    chosen = _pick_products(matches)
    if chosen is None:
        console.print("\n[yellow]Aborted. Nothing was added.[/]")
        return
    if not chosen:
        console.print("\n[yellow]Nothing selected.[/]")
        return
    _confirm_and_add(coop, chosen)


def _pick_products(matches: list[Match]) -> list[tuple[Match, Product]] | None:
    """Prompt once per ingredient. Returns ``None`` if the user aborted."""
    chosen: list[tuple[Match, Product]] = []
    for index, match in enumerate(matches, start=1):
        console.print(
            f"\n[bold]({index}/{len(matches)}) {match.line}[/]  [dim]-> {match.term}[/]"
        )
        options = match.candidates
        if not options:
            reason = f" - {match.note}" if match.note else ""
            console.print(f"  [yellow]no products found[/]{reason}")
            continue

        _print_options(options)
        picked = _prompt_choice(len(options))
        if picked == "quit":
            return None
        if picked == "skip":
            console.print("  [dim]skipped[/]")
            continue
        chosen.append((match, options[picked - 1]))
    return chosen


def _print_options(options: list[Product]) -> None:
    for number, product in enumerate(options, start=1):
        price = f"CHF {product.price:.2f}" if product.price is not None else "n/a"
        size = f" · {product.quantity}" if product.quantity else ""
        unit = f" · {product.unit_price}" if product.unit_price else ""
        console.print(
            f"  [cyan]{number:>2}[/] {product.name}  [dim]{price}{size}{unit}[/]"
        )


def _confirm_and_add(coop: CoopClient, chosen: list[tuple[Match, Product]]) -> None:
    """Show the basket, then write it only after an explicit yes."""
    console.print("\n[bold]Selected:[/]")
    total = 0.0
    for match, product in chosen:
        price = f"CHF {product.price:.2f}" if product.price is not None else "n/a"
        total += product.price or 0.0
        console.print(f"  {match.term}: {product.name} [dim]({price})[/]")
    console.print(f"  [dim]total ~CHF {total:.2f}[/]")

    noun = "product" if len(chosen) == 1 else "products"
    if not typer.confirm(
        f"\nAdd {len(chosen)} {noun} to your shopping list?", default=False
    ):
        console.print("[yellow]Nothing was added.[/]")
        return

    results = coop.add_many([product for _, product in chosen])
    for _, product in chosen:
        state = "added" if results.get(product.id) else "already on list"
        console.print(f"  {state}: {product.name}")


def _prompt_choice(count: int) -> int | str:
    """Ask which numbered option to take; ``s`` skips the ingredient, ``q`` aborts."""
    while True:
        raw = (
            typer.prompt("  pick [number / s=skip / q=quit]", default="1")
            .strip()
            .lower()
        )
        if raw in ("q", "quit"):
            return "quit"
        if raw in ("s", "skip", ""):
            return "skip"
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        console.print(f"  [red]Enter 1-{count}, or s / q.[/]")


def _render_matches(matches: list[Match], *, filtered: bool) -> None:
    table = Table(title=f"{len(matches)} ingredients", header_style="bold")
    table.add_column("Ingredient")
    table.add_column("Amount", no_wrap=True)
    table.add_column("Search term", style="dim")
    table.add_column("Best match")
    table.add_column("Price", justify="right", no_wrap=True)
    reason = "no match under these filters" if filtered else "no match"
    for m in matches:
        amount = str(m.amount) or "-"
        if m.is_spoon_scale:
            amount = f"[cyan]{amount}[/]"
        if m.best:
            price = f"{m.best.price:.2f}" if m.best.price is not None else "-"
            table.add_row(m.line, amount, m.term, f"[{m.best.id}] {m.best.name}", price)
        else:
            table.add_row(m.line, amount, m.term, f"[red]{reason}[/]", "-")
    console.print(table)
    if any(m.is_spoon_scale for m in matches):
        console.print(
            "[dim]  [cyan]cyan[/] amounts are spoon- or pinch-scale — no pack size "
            "matches them, so you may already have these in. Use --skip-pantry to "
            "leave them out.[/]"
        )


def _render_notes(matches: list[Match], candidates: int) -> None:
    for m in matches:
        if m.note:
            console.print(f"[dim]  {m.term!r}: {m.note}[/]")
    # A preference that quietly did not apply would otherwise look like it had.
    for m in matches:
        if m.best and m.preference and not m.preference_applied:
            console.print(
                f"[yellow]  no '{m.preference}' option for {m.term!r} - "
                "used the normal best match[/]"
            )
    for m in matches:
        if m.alternatives:
            alts = "; ".join(f"[{a.id}] {a.name}" for a in m.alternatives[:candidates])
            console.print(f"[dim]  alternatives for {m.term!r}: {alts}[/]")


def _apply_matches(coop: CoopClient, matches: list[Match]) -> None:
    console.print()
    for m in matches:
        if not m.best:
            console.print(f"  [red]skipped[/] {m.line} (no match)")
    resolved = [m.best for m in matches if m.best]
    results = coop.add_many(resolved)
    for product in resolved:
        state = "added" if results.get(product.id) else "already on list"
        console.print(f"  {state}: {product.name}")


def main() -> None:
    """Entry point. Maps failures onto the exit codes documented in `Exit`."""
    try:
        app()
    except NotLoggedIn as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(Exit.NOT_LOGGED_IN) from exc
    except BotWall as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(Exit.BLOCKED) from exc
    except GroceryError as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(Exit.ERROR) from exc
    except KeyboardInterrupt:
        err.print("[yellow]Interrupted.[/]")
        raise typer.Exit(Exit.ERROR) from None


if __name__ == "__main__":
    main()

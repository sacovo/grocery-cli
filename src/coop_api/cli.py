"""Command line interface: `coop search`, `coop list`, `coop add`, `coop recipe`."""

from __future__ import annotations

import pathlib
import sys

import typer
from rich.console import Console
from rich.table import Table

from .client import CoopClient
from .filters import SearchFilters
from .recipes import Match, parse_recipe, resolve
from .session import CoopError

app = typer.Typer(
    help="Search coop.ch and manage your shopping list.", no_args_is_help=True
)
console = Console()

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
) -> None:
    """Search products by free text, optionally filtered."""
    filters = _filters(vegan, vegetarian, lactose_free, gluten_free, bio)
    with CoopClient() as coop:
        hits = coop.search(query, limit=limit, category=category, filters=filters)
    if not hits:
        console.print(f"[yellow]No products found for {query!r} with those filters.[/]")
        raise typer.Exit(1)
    label = ", ".join(filter(None, [category, _filter_label(filters)]))
    title = f"{len(hits)} results for {query!r}" + (f" [{label}]" if label else "")
    console.print(_products_table(hits, title))


@app.command()
def categories(query: str) -> None:
    """Show category codes Coop suggests for a search term."""
    with CoopClient() as coop:
        found = coop.suggest_categories(query)
    if not found:
        console.print(f"[yellow]No categories suggested for {query!r}.[/]")
        raise typer.Exit(1)
    table = Table(title=f"Categories for {query!r}", header_style="bold")
    table.add_column("Code", style="dim", no_wrap=True)
    table.add_column("Name")
    for c in found:
        table.add_row(c["code"], c["name"])
    console.print(table)


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
def show_list() -> None:
    """Show the active Coop shopping list."""
    with CoopClient() as coop:
        items = coop.get_list()
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
def lists() -> None:
    """Show all shopping lists; the active one is marked."""
    with CoopClient() as coop:
        found = coop.get_lists()
    table = Table(title=f"Shopping lists ({len(found)})", header_style="bold")
    table.add_column("", no_wrap=True)
    table.add_column("Name")
    table.add_column("ID", style="dim", no_wrap=True)
    for lst in found:
        table.add_row("[green]*[/]" if lst.active else "", lst.name, lst.id)
    console.print(table)
    console.print("[dim]* = active. Switch with `coop use <name>`.[/]")


@app.command()
def use(name_or_id: str) -> None:
    """Make a shopping list the active one (matched by name or id)."""
    with CoopClient() as coop:
        selected = coop.select_list(name_or_id)
        count = len(coop.get_list())
    console.print(f"Active list is now [bold]{selected.name}[/] ({count} items).")


@app.command("new-list")
def new_list(name: str) -> None:
    """Create a new shopping list. Coop makes the new list active."""
    with CoopClient() as coop:
        created = coop.create_list(name)
    console.print(
        f"Created [bold]{created.name}[/] ({created.id}) — it is now the active list."
    )


@app.command()
def product(product_id: str) -> None:
    """Show a product's details: ingredients, allergens, nutrition."""
    with CoopClient() as coop:
        d = coop.get_product(product_id)

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
def add(product_ids: list[str]) -> None:
    """Add products to the shopping list by ID."""
    with CoopClient() as coop:
        for pid in product_ids:
            added = coop.add_to_list(pid)
            console.print(f"  {pid}: {'added' if added else 'already on list'}")


@app.command()
def remove(product_ids: list[str]) -> None:
    """Remove products from the shopping list by ID."""
    with CoopClient() as coop:
        for pid in product_ids:
            gone = coop.remove_from_list(pid)
            console.print(f"  {pid}: {'removed' if gone else 'was not on list'}")


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
    vegan: bool = VeganOpt,
    vegetarian: bool = VegetarianOpt,
    lactose_free: bool = LactoseOpt,
    gluten_free: bool = GlutenOpt,
    bio: bool = BioOpt,
) -> None:
    """Resolve recipe ingredients to Coop products.

    Prints matches only; pass --add to write them to the shopping list. Dietary flags
    apply to every ingredient, which is the point when cooking for guests.
    """
    text = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    lines = parse_recipe(text)
    if not lines:
        console.print("[yellow]No ingredients found.[/]")
        raise typer.Exit(1)

    filters = _filters(vegan, vegetarian, lactose_free, gluten_free, bio)
    if label := _filter_label(filters):
        console.print(f"[dim]Filters: {label}[/]\n")

    with CoopClient() as coop:
        matches = resolve(coop, lines, candidates=candidates + 1, filters=filters)
        _render_matches(matches, filtered=bool(filters))
        _render_notes(matches, candidates)

        if not add_to_list:
            console.print(
                "\n[dim]Nothing was added. Re-run with --add to write to your list.[/]"
            )
            return
        _apply_matches(coop, matches)


def _render_matches(matches: list[Match], *, filtered: bool) -> None:
    table = Table(title=f"{len(matches)} ingredients", header_style="bold")
    table.add_column("Ingredient")
    table.add_column("Search term", style="dim")
    table.add_column("Best match")
    table.add_column("Price", justify="right", no_wrap=True)
    reason = "no match under these filters" if filtered else "no match"
    for m in matches:
        if m.best:
            price = f"{m.best.price:.2f}" if m.best.price is not None else "-"
            table.add_row(m.line, m.term, f"[{m.best.id}] {m.best.name}", price)
        else:
            table.add_row(m.line, m.term, f"[red]{reason}[/]", "-")
    console.print(table)


def _render_notes(matches: list[Match], candidates: int) -> None:
    for m in matches:
        if m.note:
            console.print(f"[dim]  {m.term!r}: {m.note}[/]")
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
    try:
        app()
    except CoopError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc


if __name__ == "__main__":
    main()

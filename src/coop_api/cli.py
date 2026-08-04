"""Command line interface: `coop search`, `coop list`, `coop add`, `coop recipe`."""

from __future__ import annotations

import pathlib
import sys

import typer
from rich.console import Console
from rich.table import Table

from .client import CoopClient
from .filters import SearchFilters, normalise_preference
from .models import Product
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
    text = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    lines = parse_recipe(text)
    if not lines:
        console.print("[yellow]No ingredients found.[/]")
        raise typer.Exit(1)

    try:
        prefer = normalise_preference(prefer)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    filters = _filters(vegan, vegetarian, lactose_free, gluten_free, bio)
    banner = ", ".join(
        filter(None, [_filter_label(filters), f"prefer {prefer}" if prefer else ""])
    )
    if banner:
        console.print(f"[dim]{banner}[/]\n")

    with CoopClient() as coop:
        # Interactive mode shows a menu, so it needs more than the usual few.
        wanted = max(candidates + 1, 8) if interactive else candidates + 1
        matches = resolve(
            coop, lines, candidates=wanted, filters=filters, prefer=prefer
        )

        if skip_pantry:
            skipped = [m for m in matches if m.is_spoon_scale]
            if skipped:
                names = ", ".join(m.term for m in skipped)
                console.print(f"[dim]Skipped {len(skipped)} pantry-scale: {names}[/]\n")
            matches = [m for m in matches if not m.is_spoon_scale]
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
    try:
        app()
    except CoopError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc


if __name__ == "__main__":
    main()

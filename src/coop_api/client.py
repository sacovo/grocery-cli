"""High-level coop.ch client: search products, put them on a shopping list."""

from __future__ import annotations

import contextlib
import re
import urllib.parse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import extractors as js
from .filters import SearchFilters
from .models import ListItem, Product, ProductDetail, ShoppingList, parse_price
from .session import BASE, CoopError, CoopSession

# Opens the "Alle Listen" modal, the entry point for both selecting and creating.
_LIST_CHOOSER_TRIGGER = (
    'a[href*="shopping-list/list/choose/modal"], '
    '[data-modal-src*="shopping-list/list/choose/modal"]'
)


class CoopClient:
    """Search coop.ch and manage the shopping list.

    Usage::

        with CoopClient() as coop:
            hits = coop.search("Milch 3.5%")
            coop.add_to_list(hits[0])
    """

    def __init__(
        self, session: CoopSession | None = None, locale: str = "de", **kwargs
    ) -> None:
        self.locale = locale
        self._session = session or CoopSession(**kwargs)
        self._owns_session = session is None

    def __enter__(self) -> CoopClient:
        self._session.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owns_session:
            self._session.close()

    @property
    def session(self) -> CoopSession:
        return self._session

    # -- reading ----------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 30,
        category: str | None = None,
        filters: SearchFilters | None = None,
        **filter_kwargs,
    ) -> list[Product]:
        """Search products by free text, most relevant first.

        ``category`` scopes the search to a category code (e.g. ``"m_0056"``), which
        is the most effective way to stop "Parmesan" matching risotto seasoning.
        Dietary filters can be passed inline::

            coop.search("Milch", vegan=True)
            coop.search("Pasta", category="m_0187", gluten_free=True)
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        filters = self._merge_filters(filters, filter_kwargs)

        if category:
            params = urllib.parse.urlencode(
                {"q": f"{query}:relevance", "pageSize": max(limit, 30)}
            )
            url = f"{BASE}/{self.locale}/c/{category}?{params}"
        else:
            params = urllib.parse.urlencode({"text": query, "pageSize": max(limit, 30)})
            url = f"{BASE}/{self.locale}/search/?{params}"

        self._session.goto(url)
        if not self._wait_for_lister():
            return []
        self._apply_filters(filters)
        return self._collect(limit)

    def category(
        self,
        code: str,
        limit: int = 30,
        filters: SearchFilters | None = None,
        **filter_kwargs,
    ) -> list[Product]:
        """List products in a category, e.g. ``"m_0056"`` (Milch)."""
        params = urllib.parse.urlencode({"pageSize": max(limit, 30)})
        url = f"{BASE}/{self.locale}/c/{code}?{params}"
        self._session.goto(url)
        if not self._wait_for_lister():
            return []
        self._apply_filters(self._merge_filters(filters, filter_kwargs))
        return self._collect(limit)

    def suggest_categories(self, query: str) -> list[dict[str, str]]:
        """Categories Coop offers alongside the results for ``query``.

        Useful for narrowing a vague ingredient: searching "Parmesan" suggests the
        cheese categories, which you can then pass as ``category=``.
        """
        params = urllib.parse.urlencode({"text": query})
        self._session.goto(f"{BASE}/{self.locale}/search/?{params}")
        if not self._wait_for_lister():
            return []
        seen: dict[str, str] = {}
        for entry in self._session.page.evaluate(js.CATEGORIES):
            seen.setdefault(entry["code"], entry["name"])
        return [{"code": code, "name": name} for code, name in seen.items()]

    def _wait_for_lister(self, timeout: float = 20000) -> bool:
        """Wait for the result lister; False when the search matched nothing at all.

        Coop has two distinct empty states. A term it understands but has no products
        for (e.g. "Chiliflocken") still renders the lister, just with no tiles. A term
        it does not recognise at all gets a generic page with no lister and only
        recommendation carousels. Waiting on a product tile would hang for the full
        timeout on both, so wait on the lister and treat its absence as "no results".
        """
        try:
            self._session.page.wait_for_selector(js.RESULT_LISTER, timeout=timeout)
        except PlaywrightTimeoutError:
            return False
        return True

    def _merge_filters(
        self, filters: SearchFilters | None, kwargs: dict
    ) -> SearchFilters:
        if filters is not None and kwargs:
            raise TypeError(
                "Pass either a SearchFilters instance or keyword filters, not both."
            )
        if filters is not None:
            return filters
        return SearchFilters.from_kwargs(**kwargs)

    def _apply_filters(self, filters: SearchFilters) -> None:
        """Tick the filter-panel checkboxes for ``filters``.

        Coop applies facets through its own JavaScript, so each box is clicked and the
        result count is watched. A filter that silently fails to apply would hand back
        products violating a dietary restriction, so a checkbox that cannot be found
        or does not change anything raises instead.
        """
        page = self._session.page
        for checkbox_id in filters.checkbox_ids():
            before = page.evaluate(js.RESULT_COUNT)
            clicked = page.evaluate(js.TICK_FACET, checkbox_id)
            if not clicked:
                raise CoopError(
                    f"Filter {checkbox_id!r} is not offered for this search. Coop only lists "
                    "facets that match at least one result - try a broader query."
                )
            self._wait_for_count_change(before)

    def _wait_for_count_change(
        self, before: str | None, timeout: float = 15000
    ) -> None:
        page = self._session.page
        deadline = timeout
        while deadline > 0:
            page.wait_for_timeout(500)
            deadline -= 500
            if page.evaluate(js.RESULT_COUNT) != before:
                page.wait_for_timeout(1200)  # let the tile list settle
                return
        # Count may legitimately be unchanged if the facet excludes nothing.
        page.wait_for_timeout(1000)

    def _collect(self, limit: int) -> list[Product]:
        raw = self._session.page.evaluate(js.TILES)
        products: list[Product] = []
        for r in raw[:limit]:
            products.append(
                Product(
                    id=r["id"],
                    name=r["name"],
                    brand=r.get("brand"),
                    price=parse_price(r.get("price")),
                    quantity=r.get("quantity"),
                    unit_price=r.get("unitPrice"),
                    url=r.get("url"),
                    in_stock=bool(r.get("inStock")),
                    orderable=bool(r.get("orderable")),
                    rating_count=int(r["ratingCount"])
                    if r.get("ratingCount")
                    else None,
                )
            )
        return products

    # -- writing ----------------------------------------------------------------

    def get_product(self, product: Product | str) -> ProductDetail:
        """Fetch the full detail page for a product: ingredients, allergens, nutrition."""
        product_id = product.id if isinstance(product, Product) else str(product)
        self._open_product(product_id)
        self._session.page.wait_for_timeout(1200)
        raw = self._session.page.evaluate(js.PRODUCT)

        sections = dict(raw.get("sections") or {})
        ld = raw.get("ld") or {}
        offers = ld.get("offers") or {}

        # Consumed into dedicated fields; whatever is left stays in `info`.
        # The ingredient block ends with a UI note pointing at the bold markup; that
        # note is not an ingredient, and we already parsed the bold parts as allergens.
        ingredients = sections.pop("Zutaten", None)
        if ingredients:
            ingredients = (
                re.sub(r"\s*Allergiker-Infos:.*$", "", ingredients).strip() or None
            )
        origin = sections.pop("Rohstoffherkunft", None)
        country = sections.pop("Produktionsland", None)
        storage = sections.pop("Lagerung", None)
        description = sections.pop("Beschreibung", None) or ld.get("description")
        sections.pop("Nährwerte", None)  # only ever a "see details" link

        return ProductDetail(
            id=str(ld.get("productID") or product_id),
            name=ld.get("name") or raw.get("name") or "",
            brand=(ld.get("brand") or {}).get("name")
            if isinstance(ld.get("brand"), dict)
            else None,
            price=parse_price(str(offers.get("price")))
            if offers.get("price") is not None
            else None,
            description=description,
            gtin=ld.get("gtin8") or ld.get("gtin13") or ld.get("gtin"),
            ingredients=ingredients,
            allergens=list(raw.get("allergens") or []),
            origin=origin,
            production_country=country,
            storage=storage,
            nutrition=[(a, b) for a, b in raw.get("nutrition") or []],
            nutrition_base=raw.get("nutritionBase"),
            info=sections,
            url=raw.get("url"),
        )

    # -- shopping lists ---------------------------------------------------------

    def get_lists(self) -> list[ShoppingList]:
        """All of the user's shopping lists, with the active one flagged."""
        self._session.goto(f"{BASE}/{self.locale}/shopping-list")
        self._session.require_login()
        html = self._session.page.evaluate(js.FETCH_LIST_CHOOSER, self.locale)
        entries = self._session.page.evaluate(js.SHOPPING_LISTS, html)
        active = self.active_list_id()
        return [
            ShoppingList(id=e["id"], name=e["name"], active=e["id"] == active)
            for e in entries
        ]

    def active_list_id(self) -> str | None:
        """The id of the list currently shown on the shopping-list page."""
        # The toolbar carrying listId renders after the shell, so wait for it rather
        # than reading early and concluding there is no active list.
        try:
            self._session.page.wait_for_selector('a[href*="listId="]', timeout=20000)
        except PlaywrightTimeoutError:
            return None
        return self._session.page.evaluate(js.ACTIVE_LIST_ID)

    def select_list(self, name_or_id: str) -> ShoppingList:
        """Make a list the active one, by name (case-insensitive) or id.

        Subsequent `get_list` / `add_to_list` calls act on this list.
        """
        target = self._find_list(name_or_id)
        if target.active:
            return target

        self._open_list_chooser()
        self._session.page.wait_for_selector(
            f'[data-shopping-list-id="{target.id}"]', timeout=20000
        )
        self._session.page.locator(
            f'[data-shopping-list-id="{target.id}"]'
        ).first.click()
        self._session.page.wait_for_timeout(4000)

        self._session.goto(f"{BASE}/{self.locale}/shopping-list")
        if self.active_list_id() != target.id:
            raise CoopError(
                f"Tried to select list {target.name!r} but it did not become active."
            )
        return ShoppingList(id=target.id, name=target.name, active=True)

    @contextlib.contextmanager
    def using_list(self, name_or_id: str | None):
        """Temporarily make a list active, restoring the previous one afterwards.

        Reading a non-active list means switching to it, which is a write to the user's
        account. Anything read-only should leave the active list as it found it.
        """
        if not name_or_id:
            yield
            return
        previous = next((lst for lst in self.get_lists() if lst.active), None)
        self.select_list(name_or_id)
        try:
            yield
        finally:
            if previous is not None and self.active_list_id() != previous.id:
                self.select_list(previous.id)

    def create_list(self, name: str) -> ShoppingList:
        """Create a new shopping list.

        Coop happily creates several lists with the same name, so this refuses to add a
        duplicate rather than leaving you with two lists you cannot tell apart.
        """
        name = name.strip()
        if not name:
            raise ValueError("List name must not be empty.")
        existing = self.get_lists()
        if any(lst.name.casefold() == name.casefold() for lst in existing):
            raise CoopError(f"A list named {name!r} already exists.")

        page = self._session.page
        self._open_list_chooser()
        page.wait_for_selector(
            '[data-modal-src*="showWishlistCreateModal"]', timeout=20000
        )
        page.locator('[data-modal-src*="showWishlistCreateModal"]').first.click()
        page.wait_for_selector('input[name="wishlistName"]', timeout=20000)
        page.locator('input[name="wishlistName"]').first.fill(name)
        # The submit control is a bare <button> - it has no type *attribute*, so a
        # button[type="submit"] selector does not match it.
        page.locator("form#coopathomeWishlistCreateNewListForm button").first.click()
        page.wait_for_timeout(4000)

        for lst in self.get_lists():
            if lst.name.casefold() == name.casefold():
                return lst
        raise CoopError(
            f"Created list {name!r} but it did not show up in the list overview."
        )

    def _open_list_chooser(self) -> None:
        """Open the "Alle Listen" modal from the shopping-list page."""
        self._session.page.locator(_LIST_CHOOSER_TRIGGER).first.click()

    def _find_list(self, name_or_id: str) -> ShoppingList:
        lists = self.get_lists()
        needle = name_or_id.strip().casefold()
        for lst in lists:
            if lst.id.casefold() == needle or lst.name.casefold() == needle:
                return lst
        matches = [lst for lst in lists if needle in lst.name.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CoopError(
                f"{name_or_id!r} matches several lists: {[m.name for m in matches]}. "
                "Be more specific."
            )
        raise CoopError(
            f"No shopping list matching {name_or_id!r}. Available: {[lst.name for lst in lists]}"
        )

    def get_list(self) -> list[ListItem]:
        """Read the currently active Coop shopping list."""
        self._session.goto(f"{BASE}/{self.locale}/shopping-list")
        self._session.require_login()
        self._wait_for_list_render()
        items: list[ListItem] = []
        for raw in self._session.page.evaluate(js.LIST_ITEMS):
            qty = raw.get("quantity") or ""
            digits = "".join(ch for ch in qty if ch.isdigit())
            items.append(
                ListItem(
                    product_id=raw["productId"],
                    entry_id=raw["entryId"],
                    name=raw["name"],
                    quantity=int(digits) if digits else 1,
                    price=parse_price(raw.get("price")),
                )
            )
        return items

    def _wait_for_list_render(self, timeout: float = 30000) -> None:
        """Block until the shopping list has actually rendered.

        A fixed sleep is not enough - a long list can take several seconds, and reading
        too early yields zero rows. That is worse than a slow read: `add_to_list` treats
        an empty list as "not present yet" and would re-add products already on it. So
        an empty result is only accepted once the list UI itself is up.
        """
        page = self._session.page
        try:
            page.wait_for_selector("li.shoppingList__item", timeout=timeout)
            return
        except PlaywrightTimeoutError:
            pass

        # No rows yet. Trust "empty" only if the list shell rendered without them.
        try:
            page.wait_for_selector('[data-testauto="tab-shoppinglist"]', timeout=10000)
        except PlaywrightTimeoutError:
            raise CoopError(
                "The shopping list did not render within "
                f"{timeout / 1000:g}s. Refusing to report it as empty, since that would "
                "make add_to_list duplicate products already on the list."
            ) from None
        page.wait_for_timeout(1500)
        if page.locator("li.shoppingList__item").count():
            return

    def add_to_list(self, product: Product | str) -> bool:
        """Put a product on the active Coop shopping list.

        Returns ``True`` if it was added, ``False`` if it was already there.

        The control on the site is a toggle whose label does *not* reflect the current
        state, so clicking blindly can remove an item or double-count it. The list
        itself is therefore the source of truth: we look before we click, and confirm
        afterwards.
        """
        product_id = product.id if isinstance(product, Product) else str(product)
        if any(item.product_id == product_id for item in self.get_list()):
            return False
        self._click_add(product_id)
        if not any(item.product_id == product_id for item in self.get_list()):
            raise CoopError(
                f"Clicked the shopping-list control for {product_id} but it did not appear "
                "on the list. Coop may have changed the flow."
            )
        return True

    def _click_add(self, product_id: str) -> None:
        """Open a product page and hit its add-to-list control."""
        self._open_product(product_id)
        self._session.require_login()
        button = self._session.page.locator(
            f'[data-shopping-list-toggle][data-testauto$="{product_id}"]'
        ).first
        if not button.count():
            button = self._session.page.locator("[data-shopping-list-toggle]").first
        if not button.count():
            raise CoopError(f"No shopping-list control found for product {product_id}.")
        button.click()
        self._session.page.wait_for_timeout(2000)

    def remove_from_list(self, product: Product | str) -> bool:
        """Take a product off the active shopping list.

        Returns ``True`` if it was removed, ``False`` if it was not on the list.
        """
        product_id = product.id if isinstance(product, Product) else str(product)
        if not any(item.product_id == product_id for item in self.get_list()):
            return False

        # The product page's toggle only ever adds; removal lives on the list row.
        delete_icon = self._session.page.locator(
            f'li.shoppingList__item[data-productid="{product_id}"] [data-shopping-list-item-remove]'
        ).first
        if not delete_icon.count():
            raise CoopError(
                f"No delete control found on the list row for {product_id}."
            )
        delete_icon.click()
        self._session.page.wait_for_timeout(2500)

        if any(item.product_id == product_id for item in self.get_list()):
            raise CoopError(
                f"Clicked the shopping-list control for {product_id} but it is still on "
                "the list. Coop may have changed the flow."
            )
        return True

    def add_many(self, products: list[Product | str]) -> dict[str, bool]:
        """Add several products, skipping any already on the list.

        Reads the list once up front and once at the end, rather than twice per
        product - which matters when adding a whole recipe.
        """
        present = {item.product_id for item in self.get_list()}
        results: dict[str, bool] = {}
        added: list[str] = []
        for product in products:
            pid = product.id if isinstance(product, Product) else str(product)
            if pid in present:
                results[pid] = False
                continue
            self._click_add(pid)
            present.add(pid)
            added.append(pid)
            results[pid] = True

        if added:
            final = {item.product_id for item in self.get_list()}
            missing = [pid for pid in added if pid not in final]
            if missing:
                raise CoopError(
                    f"These products were clicked but did not reach the list: {missing}. "
                    "Coop may have changed the flow."
                )
        return results

    def _open_product(self, product_id: str) -> None:
        """Open a product's page. Coop resolves the short /p/<id> form for us."""
        self._session.goto(
            f"{BASE}/{self.locale}/p/{product_id}",
            wait_for="[data-shopping-list-toggle]",
        )

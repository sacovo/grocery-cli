"""migros.ch backend.

Search, prices and the shopping list go through migros.ch's own JSON API, called from
the page so the browser session and OAuth token apply. That makes it far cheaper than
the Coop backend: product cards come back in batches, and the write endpoint returns the
updated list, so there is no read-back race to lose.

Ingredients and nutrition are the exception - no endpoint the site calls exposes them,
so `get_product` scrapes the product page for those (see `extractors`).

Migros calls it a shopping list, but it *is* the basket: items on the list show up as
"im Warenkorb" and feed checkout directly. Nothing here ever checks out.
"""

from __future__ import annotations

from ...errors import GroceryError
from ...models import ListItem, Product, ProductDetail, ShoppingList, parse_price
from ..base import Capabilities
from . import extractors as js
from .session import BASE, MigrosSession

# Dietary keywords -> the facet Migros exposes. `product_special_nutrition` values come
# straight from the `filters` block the search response returns.
DIETARY_FACETS: dict[str, tuple[str, str]] = {
    "vegan": ("product_special_nutrition", "vegan"),
    "vegetarian": ("product_special_nutrition", "vegetarian"),
    "lactose_free": ("product_special_nutrition", "lactose-free"),
    "gluten_free": ("product_special_nutrition", "gluten-free"),
}
BIO_FACET = "product_bio"

# Migros labels these fields inconsistently across product types; most specific first,
# so a generic "Aufbewahrung" does not shadow the real storage instructions.
STORAGE_KEYS = (
    "Aufbewahrungshinweise für den Konsumenten",
    "Lagerhinweis",
    "Lagerung",
    "Aufbewahrung",
)
ORIGIN_KEYS = ("Herkunft", "Produktionsland", "Herkunftsland")


def _take(info: dict[str, str], keys: tuple[str, ...]) -> str | None:
    """Pop the first of `keys` present, so it is not duplicated in `info`."""
    for key in keys:
        if key in info:
            return info.pop(key)
    return None


# Language comes from the Accept-Language header. The `language` field the SPA puts in
# the request body is ignored by the API - send only that and you get English names
# back regardless, which is a quiet way to end up matching recipes against the wrong
# vocabulary.
_JS_API = """
async ({auth, method, path, body, locale}) => {
    const headers = {
        'Accept': 'application/json',
        'Accept-Language': locale + '-CH,' + locale,
        'Authorization': auth,
    };
    const init = {method, headers};
    if (body !== null) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
    }
    const r = await fetch(path, init);
    const text = await r.text();
    let parsed = null;
    try { parsed = text ? JSON.parse(text) : null; } catch (e) { /* keep raw */ }
    return {status: r.status, json: parsed, raw: parsed === null ? text.slice(0, 300) : null};
}
"""


class MigrosClient:
    """Search migros.ch and manage the shopping list that backs the basket."""

    name = "migros"
    capabilities = Capabilities(
        lists=True,
        multiple_lists=True,
        dietary_filters=True,
        categories=True,
        product_details=True,
    )

    def __init__(
        self,
        session: MigrosSession | None = None,
        locale: str = "de",
        warehouse_id: int = 2,
        **kwargs,
    ) -> None:
        self.locale = locale
        self.warehouse_id = warehouse_id
        self._session = session or MigrosSession(locale=locale, **kwargs)
        self._owns_session = session is None
        self._list_id: int | None = None

    def __enter__(self) -> MigrosClient:
        self._session.start()
        try:
            self._session.authenticate()
        except BaseException:
            # __exit__ never runs when __enter__ raises, so close here or the
            # started session (and its Playwright event loop) leaks.
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owns_session:
            self._session.close()

    @property
    def session(self) -> MigrosSession:
        return self._session

    # -- plumbing ---------------------------------------------------------------

    def _api(self, method: str, path: str, body: dict | None = None):
        auth = self._session.authenticate()
        result = self._session.page.evaluate(
            _JS_API,
            {
                "auth": auth,
                "method": method,
                "path": path,
                "body": body,
                "locale": self.locale,
            },
        )
        if result["status"] >= 400:
            detail = result.get("raw") or result.get("json")
            raise GroceryError(f"{method} {path} failed ({result['status']}): {detail}")
        return result["json"]

    def _offer_filter(self) -> dict:
        return {"storeType": "ONLINE", "warehouseId": self.warehouse_id}

    # -- reading ----------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 30,
        *,
        vegan: bool = False,
        vegetarian: bool = False,
        lactose_free: bool = False,
        gluten_free: bool = False,
        bio: bool = False,
        **unsupported: bool,
    ) -> list[Product]:
        """Products matching free text, most relevant first.

        Dietary flags are applied by Migros' own facets, never inferred from names. If
        a facet is not offered for the query it means nothing matched it, and this
        raises rather than quietly handing back unfiltered results.
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if any(unsupported.values()):
            names = ", ".join(sorted(k for k, v in unsupported.items() if v))
            raise GroceryError(
                f"Migros has no equivalent of: {names}. Refusing rather than returning "
                "results that ignore the filter."
            )

        wanted = {
            key
            for key, on in (
                ("vegan", vegan),
                ("vegetarian", vegetarian),
                ("lactose_free", lactose_free),
                ("gluten_free", gluten_free),
            )
            if on
        }
        payload = {
            "query": query,
            "language": self.locale,
            "storeType": "ONLINE",
            "warehouseId": self.warehouse_id,
            "from": 0,
            "limit": max(limit, 24),
            "filters": self._build_filters(wanted, bio),
            "sortFields": [],
            "sortOrder": "asc",
            "searchAlgorithm": "DEFAULT",
        }
        result = self._api(
            "POST", "/product-display/public/v2/products/search", payload
        )
        if (wanted or bio) and not result.get("items"):
            self._explain_missing_facets(query, wanted, bio)
        uids = [item["id"] for item in result.get("items", [])][:limit]
        return self.product_cards(uids)

    def _build_filters(self, wanted: set[str], bio: bool) -> dict:
        filters: dict[str, list[str]] = {}
        for key in wanted:
            group, value = DIETARY_FACETS[key]
            filters.setdefault(group, []).append(value)
        if bio:
            filters[BIO_FACET] = ["true"]
        return filters

    def _explain_missing_facets(self, query: str, wanted: set[str], bio: bool) -> None:
        """Turn an empty filtered result into a message that says which facet bit."""
        available = self.available_filters(query)
        missing = [
            key
            for key in wanted
            if DIETARY_FACETS[key][1]
            not in available.get(DIETARY_FACETS[key][0], set())
        ]
        if bio and BIO_FACET not in available:
            missing.append("bio")
        if missing:
            raise GroceryError(
                f"Migros offers no {', '.join(sorted(missing))} option for {query!r} - "
                "nothing matches that restriction here."
            )

    def available_filters(self, query: str) -> dict[str, set[str]]:
        """Facet ids (and their option values) Migros offers for a query."""
        payload = {
            "query": query,
            "language": self.locale,
            "storeType": "ONLINE",
            "warehouseId": self.warehouse_id,
            "from": 0,
            "limit": 1,
            "filters": {},
            "sortFields": [],
            "sortOrder": "asc",
            "searchAlgorithm": "DEFAULT",
        }
        result = self._api(
            "POST", "/product-display/public/v2/products/search", payload
        )
        offered: dict[str, set[str]] = {}
        for entry in result.get("filters", []):
            values = {
                opt.get("value") for opt in entry.get("options", []) if opt.get("value")
            }
            offered[entry["id"]] = values
        return offered

    def product_cards(self, uids: list[int | str]) -> list[Product]:
        """Full product data for many uids in a single request."""
        if not uids:
            return []
        payload = {
            "offerFilter": self._offer_filter(),
            "productFilter": {"uids": [int(u) for u in uids]},
            "language": self.locale,
        }
        cards = self._api("POST", "/product-display/public/v4/product-cards", payload)
        return [self._to_product(card) for card in cards or []]

    def _to_product(self, card: dict) -> Product:
        offer = card.get("offer") or {}
        price = offer.get("price") or {}
        online_id = card.get("migrosOnlineId")
        return Product(
            id=str(card.get("uid") or card.get("migrosId") or online_id),
            name=card.get("name") or "",
            brand=card.get("brand"),
            price=parse_price(str(price.get("effectiveValue")))
            if price.get("effectiveValue") is not None
            else None,
            quantity=offer.get("quantity"),
            unit_price=offer.get("quantityPrice"),
            url=f"/{self.locale}/product/mo/{online_id}" if online_id else None,
            in_stock=bool(offer),
            orderable=bool(offer.get("maxOrderableQuantity")),
        )

    def get_product(
        self, product: Product | str, *, full: bool = True
    ) -> ProductDetail:
        """Detail for one product.

        The card endpoint supplies price, gtin, category and labels. Ingredients and
        nutrition are not in any API the site calls - they are rendered into the product
        page - so `full` loads that page too. That costs a navigation per product, which
        is why bulk export is slower than search.
        """
        uid = product.id if isinstance(product, Product) else str(product)
        card = self._card(uid)
        online_id = card.get("migrosOnlineId")
        url = f"/{self.locale}/product/mo/{online_id}"

        info = self._card_info(card)
        scraped = self._scrape_detail(url) if full and online_id else {}
        info.update(self._extra_info(scraped))

        offer = card.get("offer") or {}
        price = (offer.get("price") or {}).get("effectiveValue")
        gtins = card.get("gtins") or []
        return ProductDetail(
            id=str(card.get("uid") or uid),
            name=card.get("name") or "",
            brand=card.get("brand"),
            price=parse_price(str(price)) if price is not None else None,
            description=card.get("description"),
            gtin=gtins[0] if gtins else None,
            ingredients=scraped.get("ingredients"),
            allergens=list(scraped.get("allergens") or []),
            storage=_take(info, STORAGE_KEYS),
            origin=_take(info, ORIGIN_KEYS),
            nutrition=[(a, b) for a, b in scraped.get("nutrition") or []],
            nutrition_base=scraped.get("base"),
            info=info,
            url=url,
        )

    def _card(self, uid: str) -> dict:
        payload = {
            "offerFilter": self._offer_filter(),
            "productFilter": {"uids": [int(uid)]},
            "language": self.locale,
        }
        cards = self._api("POST", "/product-display/public/v4/product-cards", payload)
        if not cards:
            raise GroceryError(f"No product {uid!r} at Migros.")
        return cards[0]

    def _card_info(self, card: dict) -> dict[str, str]:
        offer = card.get("offer") or {}
        breadcrumb = " > ".join(c.get("name", "") for c in card.get("breadcrumb") or [])
        labels = [
            p.get("description")
            for p in offer.get("pictos") or []
            if p.get("description")
        ]
        return {
            k: v
            for k, v in {
                "Kategorie": breadcrumb,
                "Labels": ", ".join(labels),
                "Migros-ID": card.get("migrosId"),
                "Migros-Online-ID": card.get("migrosOnlineId"),
                "Menge": offer.get("quantity"),
            }.items()
            if v
        }

    @staticmethod
    def _extra_info(scraped: dict) -> dict[str, str]:
        """Scraped definition-list entries, minus the ones with dedicated fields."""
        extra = dict(scraped.get("info") or {})
        for key in ("Zutaten", "Allergene", "Nährwerte"):
            extra.pop(key, None)
        return extra

    def _scrape_detail(self, url: str) -> dict:
        """Read the parts of a product page that no endpoint exposes."""
        self._session.goto(f"{BASE}{url}")
        self._session.page.wait_for_timeout(3500)
        return self._session.page.evaluate(js.PRODUCT_DETAIL)

    # -- shopping list (the basket) ---------------------------------------------

    def get_lists(self) -> list[ShoppingList]:
        overview = self._api("GET", "/shopping-list/public/v1/lists/overview") or []
        active = self.active_list_id()
        return [
            ShoppingList(
                id=str(entry["shoppingListId"]),
                name=entry.get("shoppingListName") or "",
                active=str(entry["shoppingListId"]) == str(active),
            )
            for entry in overview
        ]

    def active_list_id(self) -> int:
        """The list writes go to. Migros' own UI treats the first list as current."""
        if self._list_id is None:
            overview = self._api("GET", "/shopping-list/public/v1/lists/overview") or []
            if not overview:
                raise GroceryError("This Migros account has no shopping list.")
            self._list_id = int(overview[0]["shoppingListId"])
        return self._list_id

    def select_list(self, name_or_id: str) -> ShoppingList:
        for lst in self.get_lists():
            if (
                lst.id == str(name_or_id)
                or lst.name.casefold() == str(name_or_id).casefold()
            ):
                self._list_id = int(lst.id)
                return ShoppingList(id=lst.id, name=lst.name, active=True)
        raise GroceryError(f"No Migros shopping list matching {name_or_id!r}.")

    def create_list(self, name: str) -> ShoppingList:
        """Create a shopping list.

        Refuses a duplicate name, as Coop's does: Migros will happily create two lists
        called the same thing and then you cannot tell them apart. Note there is no
        delete endpoint - `/shopping-list/public/v1/list` allows POST only - so a list
        made here can only be removed in the Migros app.
        """
        name = name.strip()
        if not name:
            raise ValueError("List name must not be empty.")
        if any(lst.name.casefold() == name.casefold() for lst in self.get_lists()):
            raise GroceryError(f"A Migros list named {name!r} already exists.")

        created = self._api("POST", "/shopping-list/public/v1/list", {"name": name})
        new_id = str((created or {}).get("shoppingListId") or "")
        if not new_id:
            raise GroceryError(f"Migros did not return an id when creating {name!r}.")
        self._list_id = int(new_id)
        return ShoppingList(id=new_id, name=name, active=True)

    def delete_list(self, name_or_id: str) -> ShoppingList:
        """Delete a shopping list permanently. There is no undo.

        Only exposed because Migros provides the endpoint; the CLI still makes the
        caller confirm, and the Coop backend deliberately has no equivalent.
        """
        target = next(
            (
                lst
                for lst in self.get_lists()
                if lst.id == str(name_or_id)
                or lst.name.casefold() == str(name_or_id).casefold()
            ),
            None,
        )
        if target is None:
            raise GroceryError(f"No Migros shopping list matching {name_or_id!r}.")

        self._api("DELETE", f"/shopping-list/public/v1/list/delete/{target.id}")
        if any(lst.id == target.id for lst in self.get_lists()):
            raise GroceryError(
                f"Migros accepted the delete but {target.name!r} remains."
            )
        if self._list_id == int(target.id):
            self._list_id = None
        return target

    def get_list(self, *, with_prices: bool = True) -> list[ListItem]:
        """The active list. Prices are looked up separately - see `_add_prices`."""
        list_id = self.active_list_id()
        details = self._api(
            "GET", f"/shopping-list/public/v2/list/details?shoppingListId={list_id}"
        )
        items = self._items_from(details)
        return self._add_prices(items) if with_prices else items

    def _add_prices(self, items: list[ListItem]) -> list[ListItem]:
        """Fill in prices, which the shopping-list API does not return.

        One batched product-cards call covers the whole list, so this stays cheap even
        for a long list - without it an export has an empty price column.
        """
        if not items:
            return items
        try:
            cards = {p.id: p for p in self.product_cards([i.product_id for i in items])}
        except GroceryError:
            return items
        for item in items:
            card = cards.get(item.product_id)
            if card is not None:
                item.price = card.price
        return items

    def _items_from(self, details: dict | None) -> list[ListItem]:
        items: list[ListItem] = []
        for category in (details or {}).get("categories", []):
            for raw in category.get("items", []):
                items.append(
                    ListItem(
                        product_id=str(raw.get("id")),
                        entry_id=str(raw.get("id")),
                        name=raw.get("name") or "",
                        quantity=int(raw.get("quantity") or 1),
                        price=None,
                    )
                )
        return items

    def _put_items(self, items: list[dict]) -> list[ListItem]:
        """Single mutation endpoint: add, change quantity, or remove with quantity 0.

        Returns the updated list straight from the response, so there is never a stale
        read between writing and checking.
        """
        payload = {"shoppingListId": self.active_list_id(), "items": items}
        return self._items_from(
            self._api("PUT", "/shopping-list/public/v3/items", payload)
        )

    def add_to_list(self, product: Product | str, quantity: int = 1) -> bool:
        """Add a product. False if it was already on the list (quantity untouched)."""
        uid = product.id if isinstance(product, Product) else str(product)
        if any(item.product_id == uid for item in self.get_list()):
            return False
        after = self._put_items([{"id": uid, "quantity": quantity, "type": "PRODUCT"}])
        if not any(item.product_id == uid for item in after):
            raise GroceryError(
                f"Migros accepted the write for {uid} but it is not on the list."
            )
        return True

    def add_many(self, products: list[Product | str]) -> dict[str, bool]:
        """Add several products in one request.

        Migros takes a batch, so a whole recipe costs one round trip rather than one
        per ingredient.
        """
        present = {item.product_id for item in self.get_list()}
        wanted = [p.id if isinstance(p, Product) else str(p) for p in products]
        fresh = [uid for uid in dict.fromkeys(wanted) if uid not in present]
        if fresh:
            self._put_items(
                [{"id": uid, "quantity": 1, "type": "PRODUCT"} for uid in fresh]
            )
        return {uid: uid in fresh for uid in wanted}

    def remove_from_list(self, product: Product | str) -> bool:
        """Remove a product. False if it was not on the list."""
        uid = product.id if isinstance(product, Product) else str(product)
        if not any(item.product_id == uid for item in self.get_list()):
            return False
        after = self._put_items([{"id": uid, "quantity": 0, "type": "PRODUCT"}])
        if any(item.product_id == uid for item in after):
            raise GroceryError(
                f"Migros accepted the write for {uid} but it is still listed."
            )
        return True

    def product_url(self, product: Product | str) -> str:
        url = product.url if isinstance(product, Product) else None
        return f"{BASE}{url}" if url else BASE

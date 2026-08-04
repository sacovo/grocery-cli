"""JavaScript snippets evaluated in the page to read coop.ch's markup.

Everything that depends on Coop's HTML lives here. When a redesign breaks the client,
this is the only file that should need to change - the rest of the package deals in
`Product`, `ListItem` and friends.

Each constant is an expression evaluating to a function, ready for `page.evaluate`.
"""

from __future__ import annotations

# Selector for a product tile inside the result lister.
PRODUCT_TILE = "[data-producttile-wrapper]"

# The lister that holds genuine results. Deliberately narrow: a page with no matches
# still renders recommendation carousels (.productCarouselIsolated__item) full of
# unrelated products, and reading those as results turns "no vegan bacon" into "Heinz
# burger sauce" - exactly the wrong answer a dietary filter must never give.
RESULT_LISTER = ".list-page__content"

# One dict per product tile on a listing page.
TILES = f"""
() => {{
    const tiles = document.querySelectorAll('{RESULT_LISTER} {PRODUCT_TILE}');
    return [...tiles].map(t => {{
        const link = t.querySelector('a[data-productid]');
        const pick = sel => {{ const e = t.querySelector(sel); return e ? e.textContent.trim() : null; }};
        const unitLabel = [...t.querySelectorAll('.productTile__price-label')]
            .find(e => /Preis pro/i.test(e.textContent || ''));
        let unitPrice = null;
        if (unitLabel && unitLabel.nextElementSibling) {{
            unitPrice = unitLabel.nextElementSibling.textContent.trim().replace(/\\s+/g, '');
        }}
        const priceEl = t.querySelector('[data-udo-price]');
        const ratingTxt = pick('.rating__amount');
        return {{
            id: link ? link.getAttribute('data-productid') : null,
            name: pick('.productTile-details__name-value'),
            brand: pick('[data-udo-brand]'),
            price: priceEl ? priceEl.getAttribute('data-udo-price')
                           : pick('.productTile__price-value-lead-price'),
            quantity: pick('.productTile__quantity-text'),
            unitPrice: unitPrice,
            url: link ? (link.getAttribute('href') || '').split('?')[0] : null,
            inStock: !!t.querySelector('link[href*="InStock"]'),
            orderable: !!t.querySelector(
                'button[title*="Warenkorb" i], button[aria-label*="Warenkorb" i]'),
            ratingCount: ratingTxt ? ratingTxt.replace(/[^0-9]/g, '') : null,
        }};
    }}).filter(p => p.id && p.name);
}}
"""

# Category refinements offered alongside a set of results ("Reibkäse", "Frische Milch").
# Scoped to the result navigation - a bare a[href*="/c/m_"] also matches the global
# header and footer menus, which have nothing to do with the query.
CATEGORIES = """
() => [...document.querySelectorAll('.productListNavigation__item a[href*="/c/m_"]')].map(a => {
    const m = (a.getAttribute('href') || '').match(/\\/c\\/(m_[0-9a-z]+)/);
    const name = a.textContent.trim().replace(/\\s+/g, ' ');
    return m && name ? {code: m[1], name} : null;
}).filter(Boolean)
"""

# Number of results Coop reports for the current query, digits only.
RESULT_COUNT = """
() => {
    const m = document.body.innerText.match(/Es stehen\\s+([\\d'\\u2019]+)\\s+Produkte/);
    return m ? m[1].replace(/['\\u2019]/g, '') : null;
}
"""

# Product detail page: labelled sections, highlighted allergens, nutrition rows.
PRODUCT = """
() => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();

    const sections = {};
    document.querySelectorAll('.productInformation__title').forEach(h => {
        const body = h.nextElementSibling;
        if (body) sections[norm(h.textContent)] = norm(body.textContent);
    });

    // Coop marks allergens by bolding them inside the ingredient list.
    let allergens = [];
    document.querySelectorAll('.productInformation__title').forEach(h => {
        if (/^Zutaten/i.test(norm(h.textContent)) && h.nextElementSibling) {
            allergens = [...h.nextElementSibling.querySelectorAll('b, strong')]
                .map(b => norm(b.textContent)).filter(Boolean);
        }
    });

    const nutrition = [...document.querySelectorAll('[data-testauto="nutrition-row"]')].map(li => {
        const label = li.querySelector('.list--dotted-item__label-text');
        const amount = li.querySelector('.list--dotted-item__amount');
        return [norm(label ? label.textContent : ''), norm(amount ? amount.textContent : '')];
    }).filter(pair => pair[0]);

    const base = document.querySelector('[data-testauto="productnutritionbaseamount"]');

    let ld = null;
    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
        try {
            const parsed = JSON.parse(s.textContent);
            if (parsed && parsed['@type'] === 'Product') ld = parsed;
        } catch (e) { /* breadcrumb blocks are not always valid on their own */ }
    });

    return {
        sections,
        allergens,
        nutrition,
        nutritionBase: base ? norm(base.textContent) : null,
        ld,
        name: norm((document.querySelector('h1') || {}).textContent),
        url: location.pathname,
    };
}
"""

# One entry per row of the active shopping list.
LIST_ITEMS = """
() => [...document.querySelectorAll('li.shoppingList__item')].map(li => {
    const qty = li.querySelector('.shoppingList__description');
    const price = li.querySelector('[data-udo-price]');
    return {
        productId: li.getAttribute('data-productid'),
        entryId: li.getAttribute('data-shoppinglist--entry-id'),
        name: (li.getAttribute('aria-label') || '').trim(),
        quantity: qty ? qty.textContent.trim() : null,
        price: price ? price.getAttribute('data-udo-price') : null,
    };
}).filter(i => i.productId && i.entryId)
"""

# Parses the list-chooser modal HTML (fetched separately) into list entities.
SHOPPING_LISTS = """
(html) => {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    return [...doc.querySelectorAll('[data-shopping-list-id]')].map(e => ({
        id: e.getAttribute('data-shopping-list-id'),
        name: (e.textContent || '').replace(/\\s+/g, ' ').trim(),
    })).filter(l => l.id);
}
"""

# Id of the list currently displayed, read from any toolbar link carrying it.
ACTIVE_LIST_ID = """
() => {
    const a = document.querySelector('a[href*="listId="]');
    if (!a) return null;
    const m = (a.getAttribute('href') || '').match(/listId=([0-9A-Fa-f-]+)/);
    return m ? m[1] : null;
}
"""

# Fetches the chooser modal, which is the only place that enumerates every list.
FETCH_LIST_CHOOSER = """
async (locale) => {
    const r = await fetch(`/${locale}/shopping-list/list/choose/modal`,
                          {headers: {'X-Requested-With': 'XMLHttpRequest'}});
    return await r.text();
}
"""

# Ticks a filter-panel checkbox by id; returns false if that facet is not offered.
TICK_FACET = """
(id) => {
    const box = document.querySelector('#' + CSS.escape(id));
    if (!box) return false;
    if (!box.checked) box.click();
    return true;
}
"""

# True when DataDome served its interstitial instead of the real page.
IS_BLOCKED = """
() => !!document.querySelector('script[src*="captcha-delivery"]')
"""

# A logout link is only rendered for authenticated sessions; the account link and the
# shopping-list toggles are present anonymously too.
IS_LOGGED_IN = """
() => !!document.querySelector('a[href*="/logout"]')
"""

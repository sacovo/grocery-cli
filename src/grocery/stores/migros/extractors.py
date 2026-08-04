"""The one part of the Migros backend that reads markup instead of JSON.

Ingredients, nutrition and the "Weitere Angaben" block are rendered into the product
page and are not exposed by any API endpoint the SPA calls - so this is scraped, and
like the Coop extractors it is the piece a redesign will break first.
"""

from __future__ import annotations

# Ingredients carry a stable testid, and Migros bolds the allergens inside them exactly
# as Coop does, so allergen extraction reads markup rather than guessing from CAPS.
PRODUCT_DETAIL = """
() => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();

    const ing = document.querySelector('[data-testid$="-ingredients"]');
    const ingredients = ing ? norm(ing.textContent) : null;
    // The trailing "Allergiker-Infos" note is bolded too but is UI chrome, not an
    // ingredient - keeping it would put a sentence in the allergen list.
    const allergens = ing
        ? [...new Set([...ing.querySelectorAll('strong, b')]
            .map(e => norm(e.textContent))
            .filter(Boolean)
            .filter(t => !/^Allergiker|^Allergie|Infos:/i.test(t)))]
        : [];

    // Everything Migros presents as a definition list: "Weitere Angaben", origin, etc.
    const info = {};
    document.querySelectorAll('dl').forEach(dl => {
        const terms = [...dl.querySelectorAll('dt')];
        terms.forEach(dt => {
            const dd = dt.nextElementSibling;
            if (dd && dd.tagName === 'DD') {
                const key = norm(dt.textContent);
                const value = norm(dd.textContent);
                if (key && value && !(key in info)) info[key] = value;
            }
        });
    });

    // Nutrition lives under the #nutritional_values panel as label/value rows. Unlike
    // Coop, Migros prints the units, so values are kept verbatim and stay unambiguous.
    let nutrition = [];
    let base = null;
    const heading = document.querySelector('#nutritional_values');
    const panel = heading ? heading.closest('mat-expansion-panel') || heading.parentElement : null;
    const scope = panel ? panel.closest('mat-expansion-panel') || panel : null;
    if (scope) {
        const rows = [...scope.querySelectorAll('tr')];
        nutrition = rows.map(tr => {
            const cells = [...tr.querySelectorAll('th, td')].map(c => norm(c.textContent));
            return cells.length >= 2 ? [cells[0], cells[1]] : null;
        }).filter(Boolean).filter(pair => pair[0] && pair[1]);
        const head = norm(scope.textContent).match(/(100\\s?(?:g|ml))/i);
        base = head ? head[1] : null;
    }

    return {ingredients, allergens, info, nutrition, base};
}
"""

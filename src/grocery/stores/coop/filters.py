"""Search filters: dietary properties and categories.

Coop exposes these as facets in the filter panel. Note they are applied by ticking
checkboxes in the live page, *not* via URL parameters: the ``q=...:nutritionalProperties:VEGAN``
form that appears in the address bar is only pushState'd by the site's own JavaScript,
and a fresh request to that URL silently returns unfiltered results. Getting that wrong
is quiet and dangerous - you would serve dairy to a vegan guest - so everything here
goes through the checkboxes and verifies the result count actually moved.
"""

from __future__ import annotations

import dataclasses
from typing import cast

# Facet group -> the checkbox id prefix used in the filter panel.
NUTRITIONAL_GROUP = "nutritionalProperties"

# Dietary / allergen properties, keyword name -> Coop facet value.
DIETARY: dict[str, str] = {
    "vegan": "VEGAN",
    "vegetarian": "VEGETARIAN",
    "lactose_free": "LACTOSE_FREE",
    "gluten_free": "GLUTEN_FREE",
}

# Other nutritional facets, same group.
NUTRITIONAL: dict[str, str] = {
    "high_protein": "HIGH_PROTEIN",
    "high_fibre": "HIGH_FIBRE",
    "no_added_sugar": "NO_ADDED_SUGAR",
    "no_added_sugar_or_sweetener": "NO_ADDED_SUGAR_OR_SWEETENER",
    "low_fat": "LOW_FAT",
    "reduced_fat": "REDUCED_FAT",
    "microbial_rennet": "MICROBIAL_RENNET",
    "contains_iron": "CONTAINS_IRON",
    "contains_calcium": "CONTAINS_CALCIUM",
    "contains_vitamin_b12": "CONTAINS_VITAMIN_B12",
    "contains_vitamin_c": "CONTAINS_VITAMIN_C",
    "contains_vitamin_e": "CONTAINS_VITAMIN_E",
}

ALL_PROPERTIES = {**DIETARY, **NUTRITIONAL}

# Standalone boolean facets, keyword -> (group, value).
FLAGS: dict[str, tuple[str, str]] = {
    "bio": ("bioFacet", "true"),
    "online_only": ("availableOnline", "true"),
    "on_offer": ("specialOfferFacet", "true"),
}

# Soft preferences for picking between candidates. Unlike the flags above these are
# never hard constraints: if a preference matches nothing, the caller falls back to the
# unpreferred results rather than dropping the ingredient. A value of ``None`` means the
# preference is applied by sorting the results instead of by a facet.
PREFERENCES: dict[str, tuple[str, str] | None] = {
    "bio": ("bioFacet", "true"),
    "budget": ("petBrand", "Prix Garantie"),
    "cheapest": None,
}

_PREFERENCE_ALIASES = {
    "prix garantie": "budget",
    "prix-garantie": "budget",
    "prixgarantie": "budget",
    "günstig": "budget",
    "cheap": "cheapest",
    "organic": "bio",
    "naturaplan": "bio",
}


def normalise_preference(value: str | None) -> str | None:
    """Map user input like ``"Prix Garantie"`` onto a known preference key."""
    if value is None:
        return None
    key = value.strip().casefold()
    if not key or key == "none":
        return None
    key = _PREFERENCE_ALIASES.get(key, key)
    if key not in PREFERENCES:
        known = sorted(set(PREFERENCES) | set(_PREFERENCE_ALIASES))
        raise ValueError(f"Unknown preference {value!r}. Known: {', '.join(known)}")
    return key


@dataclasses.dataclass(slots=True)
class SearchFilters:
    """Facets to apply to a search.

    ``properties`` takes raw Coop facet values (e.g. ``"VEGAN"``) for anything not
    covered by the named flags.
    """

    vegan: bool = False
    vegetarian: bool = False
    lactose_free: bool = False
    gluten_free: bool = False
    bio: bool = False
    online_only: bool = False
    on_offer: bool = False
    properties: tuple[str, ...] = ()
    extra_facets: tuple[tuple[str, str], ...] = ()
    """Raw ``(group, value)`` facets, used to carry soft preferences."""

    def checkbox_ids(self) -> list[str]:
        """The filter-panel checkbox ids this selection corresponds to."""
        ids: list[str] = []
        for keyword, value in DIETARY.items():
            if getattr(self, keyword, False):
                ids.append(f"{NUTRITIONAL_GROUP}-{value}")
        for keyword, (group, value) in FLAGS.items():
            if getattr(self, keyword, False):
                ids.append(f"{group}-{value}")
        for raw in self.properties:
            value = ALL_PROPERTIES.get(raw.lower(), raw.upper())
            ids.append(f"{NUTRITIONAL_GROUP}-{value}")
        ids.extend(f"{group}-{value}" for group, value in self.extra_facets)
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(ids))

    def as_kwargs(self) -> dict[str, bool]:
        """The active flags as plain keywords.

        This is the calling convention every store understands: a store that cannot
        honour one of these must refuse it, never ignore it.
        """
        names = (
            "vegan",
            "vegetarian",
            "lactose_free",
            "gluten_free",
            "bio",
            "online_only",
            "on_offer",
        )
        return {name: True for name in names if getattr(self, name, False)}

    def with_facet(self, facet: tuple[str, str]) -> SearchFilters:
        """A copy with one more raw facet applied."""
        clone = dataclasses.replace(self, extra_facets=(*self.extra_facets, facet))
        return cast("SearchFilters", clone)

    def __bool__(self) -> bool:
        return bool(self.checkbox_ids())

    @classmethod
    def from_kwargs(cls, **kwargs) -> SearchFilters:
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(kwargs) - known
        if unknown:
            raise TypeError(f"Unknown filter(s): {sorted(unknown)}")
        return cls(**kwargs)

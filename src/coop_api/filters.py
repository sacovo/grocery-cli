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
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(ids))

    def __bool__(self) -> bool:
        return bool(self.checkbox_ids())

    @classmethod
    def from_kwargs(cls, **kwargs) -> SearchFilters:
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(kwargs) - known
        if unknown:
            raise TypeError(f"Unknown filter(s): {sorted(unknown)}")
        return cls(**kwargs)

"""Turn recipe ingredient lines into concrete Coop products."""

from __future__ import annotations

import dataclasses
import re

from .client import CoopClient
from .filters import PREFERENCES, SearchFilters, normalise_preference
from .models import Product
from .session import CoopError

# Leading amounts we strip before searching: "500g Spaghetti", "2 EL Olivenöl",
# "1.5 dl Rahm", "2-3 Zwiebeln".
_UNITS = (
    r"(?:g|kg|mg|ml|cl|dl|l|EL|TL|Stk\.?|Stück|Prise|Bund|Dose|Packung|Pack|Zehe|Zehen)"
)
_AMOUNT = re.compile(
    # The en dash in the character class is deliberate: recipes write quantity ranges
    # with an en dash at least as often as with a plain hyphen.
    rf"^\s*\d+(?:[.,]\d+)?(?:\s*[-–/]\s*\d+(?:[.,]\d+)?)?\s*{_UNITS}?\b\.?\s*",  # noqa: RUF001
    re.IGNORECASE,
)
_NOISE = re.compile(
    r"\b(frisch|gehackt|gehackte[rn]?|fein|grob|nach Belieben|optional|ca\.|etwas)\b",
    re.IGNORECASE,
)


def clean_ingredient(line: str) -> str:
    """Reduce a recipe line to a searchable product term.

    ``"500 g Spaghetti, fein"`` -> ``"Spaghetti"``
    """
    text = line.strip()
    text = re.sub(r"^[-*•]\s*", "", text)
    text = _AMOUNT.sub("", text)
    text = text.split(",")[0]
    text = _NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" .-")


@dataclasses.dataclass(slots=True)
class Match:
    """One resolved ingredient."""

    line: str
    term: str
    best: Product | None
    alternatives: list[Product] = dataclasses.field(default_factory=list)
    note: str | None = None
    """Why this line did not resolve, when it didn't."""
    preference: str | None = None
    """The preference that was requested for this line, if any."""
    preference_applied: bool = False
    """False when the preference matched nothing and plain results were used instead."""

    @property
    def resolved(self) -> bool:
        return self.best is not None

    @property
    def candidates(self) -> list[Product]:
        """Best match followed by the alternatives, for pickers."""
        return ([self.best] if self.best else []) + list(self.alternatives)


def _rank_candidates(hits: list[Product]) -> list[Product]:
    """Order candidates: deliverable ones first, otherwise keep Coop's relevance.

    Deliberately does *not* reward a literal substring match on the search term. Coop's
    search understands that "Parmesan" means Parmigiano Reggiano; a substring test does
    not, and scoring on it promotes "Knorr Risotto Parmesan" over actual cheese. Coop's
    ordering beats anything reconstructable from the product name, so the only thing
    worth overriding it for is whether the product can actually be delivered.
    """
    # sorted() is stable, so products with equal keys keep Coop's relevance order.
    return sorted(hits, key=lambda product: not product.orderable)


def resolve(
    coop: CoopClient,
    lines: list[str],
    candidates: int = 5,
    orderable_only: bool = True,
    filters: SearchFilters | None = None,
    prefer: str | None = None,
) -> list[Match]:
    """Search Coop for each ingredient line and rank the candidates.

    ``filters`` applies to every ingredient - pass ``SearchFilters(vegetarian=True)``
    when cooking for guests with restrictions. Nothing is written; inspect the matches,
    then add the ones you want.

    ``prefer`` is one of :data:`coop_api.filters.PREFERENCES` (``"bio"``, ``"budget"``,
    ``"cheapest"``). Filters and preferences are deliberately not the same thing:

    * A filter is a **hard constraint**. An ingredient whose filters exclude everything
      comes back unresolved, so a dietary restriction is never quietly dropped.
    * A preference is a **soft bias**. If nothing matches it, the unpreferred results are
      used and the match records ``preference_applied=False`` so the fallback is visible.
    """
    prefer = normalise_preference(prefer)
    return [
        _resolve_line(coop, line, candidates, orderable_only, filters, prefer)
        for line in lines
    ]


def _resolve_line(
    coop: CoopClient,
    line: str,
    candidates: int,
    orderable_only: bool,
    filters: SearchFilters | None,
    prefer: str | None = None,
) -> Match:
    term = clean_ingredient(line)
    if not term:
        return Match(line=line, term=term, best=None)
    try:
        hits, applied = _search_preferring(coop, term, candidates, filters, prefer)
    except CoopError as exc:
        return Match(line=line, term=term, best=None, note=str(exc), preference=prefer)
    if orderable_only:
        hits = [h for h in hits if h.orderable] or hits
    hits = _rank_candidates(hits)
    if prefer == "cheapest":
        hits = _cheapest_first(hits)
    return Match(
        line=line,
        term=term,
        best=hits[0] if hits else None,
        alternatives=hits[1:candidates],
        preference=prefer,
        preference_applied=applied and bool(hits),
    )


def _search_preferring(
    coop: CoopClient,
    term: str,
    candidates: int,
    filters: SearchFilters | None,
    prefer: str | None,
) -> tuple[list[Product], bool]:
    """Search with the preference facet, falling back to plain results.

    Returns the hits and whether the preference actually took effect. Hard filters are
    always kept; only the preference is dropped on fallback.
    """
    base = filters or SearchFilters()
    limit = max(candidates, 10)
    facet = PREFERENCES.get(prefer) if prefer else None

    if facet is not None:
        try:
            hits = coop.search(term, limit=limit, filters=base.with_facet(facet))
            if hits:
                return hits, True
        except CoopError:
            # Coop only lists facets with at least one hit, so an absent facet simply
            # means "nothing here matches the preference" - not an error worth raising.
            pass

    # "cheapest" needs no facet - it is applied by sorting, so it never falls back.
    # Any facet preference that matched nothing also lands here, and does count as
    # a fallback.
    return coop.search(term, limit=limit, filters=base), prefer == "cheapest"


def _cheapest_first(hits: list[Product]) -> list[Product]:
    """Cheapest first, keeping undeliverable items last and unpriced items at the end."""
    return sorted(
        hits,
        key=lambda p: (
            not p.orderable,
            p.price if p.price is not None else float("inf"),
        ),
    )


def parse_recipe(text: str) -> list[str]:
    """Split a recipe/ingredient blob into individual lines."""
    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

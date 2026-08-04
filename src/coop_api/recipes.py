"""Turn recipe ingredient lines into concrete Coop products."""

from __future__ import annotations

import dataclasses
import re

from .client import CoopClient
from .filters import SearchFilters
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

    @property
    def resolved(self) -> bool:
        return self.best is not None


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
) -> list[Match]:
    """Search Coop for each ingredient line and rank the candidates.

    ``filters`` applies to every ingredient - pass ``SearchFilters(vegetarian=True)``
    when cooking for guests with restrictions. Nothing is written; inspect the matches,
    then add the ones you want.

    An ingredient whose filters exclude everything comes back unresolved rather than
    falling back to an unfiltered match, so a restriction can never be quietly dropped.
    """
    return [
        _resolve_line(coop, line, candidates, orderable_only, filters) for line in lines
    ]


def _resolve_line(
    coop: CoopClient,
    line: str,
    candidates: int,
    orderable_only: bool,
    filters: SearchFilters | None,
) -> Match:
    term = clean_ingredient(line)
    if not term:
        return Match(line=line, term=term, best=None)
    try:
        hits = coop.search(term, limit=max(candidates, 10), filters=filters)
    except CoopError as exc:
        return Match(line=line, term=term, best=None, note=str(exc))
    if orderable_only:
        hits = [h for h in hits if h.orderable] or hits
    hits = _rank_candidates(hits)
    return Match(
        line=line,
        term=term,
        best=hits[0] if hits else None,
        alternatives=hits[1:candidates],
    )


def parse_recipe(text: str) -> list[str]:
    """Split a recipe/ingredient blob into individual lines."""
    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

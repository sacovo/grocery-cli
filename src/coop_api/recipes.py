"""Turn recipe ingredient lines into concrete Coop products."""

from __future__ import annotations

import dataclasses
import re

from .client import CoopClient
from .filters import PREFERENCES, SearchFilters, normalise_preference
from .models import Product
from .session import CoopError

# How a unit relates to what you can actually buy:
#   "weight"/"volume" - the amount tells you which pack size to pick.
#   "count"           - discrete items, or packaging the recipe assumes.
#   "spoon"           - spoon- and pinch-scale. See SPOON_SCALE below.
WEIGHT, VOLUME, COUNT, SPOON = "weight", "volume", "count", "spoon"

# Canonical spellings reused across the table below.
_PIECE = "Stück"

# Every spelling maps to a canonical unit plus its kind. Written out rather than
# generated because German recipe units pluralise irregularly (Zehe/Zehen,
# Scheibe/Scheiben, Glas/Gläser) and abbreviate inconsistently (EL/Essl./Esslöffel).
UNITS: dict[str, tuple[str, str]] = {
    # weight
    "mg": ("mg", WEIGHT),
    "g": ("g", WEIGHT),
    "gr": ("g", WEIGHT),
    "gramm": ("g", WEIGHT),
    "kg": ("kg", WEIGHT),
    "kilo": ("kg", WEIGHT),
    "kilogramm": ("kg", WEIGHT),
    "pfund": ("500g", WEIGHT),
    # volume
    "ml": ("ml", VOLUME),
    "cl": ("cl", VOLUME),
    "dl": ("dl", VOLUME),
    "l": ("l", VOLUME),
    "liter": ("l", VOLUME),
    # count and packaging
    "stk": (_PIECE, COUNT),
    "stk.": (_PIECE, COUNT),
    "stück": (_PIECE, COUNT),
    "st": (_PIECE, COUNT),
    "zehe": ("Zehe", COUNT),
    "zehen": ("Zehe", COUNT),
    "scheibe": ("Scheibe", COUNT),
    "scheiben": ("Scheibe", COUNT),
    "blatt": ("Blatt", COUNT),
    "blätter": ("Blatt", COUNT),
    "bund": ("Bund", COUNT),
    "zweig": ("Zweig", COUNT),
    "zweige": ("Zweig", COUNT),
    "kopf": ("Kopf", COUNT),
    "knolle": ("Knolle", COUNT),
    "dose": ("Dose", COUNT),
    "dosen": ("Dose", COUNT),
    "glas": ("Glas", COUNT),
    "gläser": ("Glas", COUNT),
    "packung": ("Packung", COUNT),
    "packungen": ("Packung", COUNT),
    "päckchen": ("Päckchen", COUNT),
    "pack": ("Packung", COUNT),
    "becher": ("Becher", COUNT),
    "tasse": ("Tasse", COUNT),
    "tassen": ("Tasse", COUNT),
    "flasche": ("Flasche", COUNT),
    "flaschen": ("Flasche", COUNT),
    # spoon and pinch scale
    "el": ("EL", SPOON),
    "essl": ("EL", SPOON),
    "esslöffel": ("EL", SPOON),
    "tl": ("TL", SPOON),
    "teel": ("TL", SPOON),
    "teelöffel": ("TL", SPOON),
    "kl": ("KL", SPOON),
    "kaffeelöffel": ("KL", SPOON),
    "msp": ("Msp", SPOON),
    "messerspitze": ("Msp", SPOON),
    "prise": ("Prise", SPOON),
    "prisen": ("Prise", SPOON),
    "spritzer": ("Spritzer", SPOON),
    "schuss": ("Schuss", SPOON),
    "handvoll": ("Handvoll", SPOON),
    "tropfen": ("Tropfen", SPOON),
}

# Units whose amount says nothing about which pack to buy: nobody sells two
# tablespoons of oil. Recipes using them usually mean a staple you already own, so
# they are flagged rather than allowed to influence the choice of product.
SPOON_SCALE = frozenset(name for name, (_, kind) in UNITS.items() if kind == SPOON)

_VULGAR_FRACTIONS = {
    "½": 0.5,
    "⅓": 1 / 3,
    "⅔": 2 / 3,
    "¼": 0.25,
    "¾": 0.75,
    "⅕": 0.2,
    "⅖": 0.4,
    "⅗": 0.6,
    "⅘": 0.8,
    "⅙": 1 / 6,
    "⅚": 5 / 6,
    "⅛": 0.125,
    "⅜": 0.375,
    "⅝": 0.625,
    "⅞": 0.875,
}

_DECIMAL = r"\d+(?:[.,]\d+)?"
_FRACTION_CHARS = "".join(_VULGAR_FRACTIONS)
# "3/4", "1½", "½", "1.5", "2" - longest alternatives first so they win.
# Each alternative is grouped: a bare "?" after _DECIMAL would attach to its own
# trailing quantifier and make it lazy instead of making the number optional.
_QUANTITY = (
    rf"(?:{_DECIMAL}\s*/\s*\d+"  # 3/4
    rf"|(?:{_DECIMAL})?\s*[{_FRACTION_CHARS}]"  # 1½ or ½
    rf"|{_DECIMAL})"  # 1.5
)
# Longest unit spellings first, so "esslöffel" is not truncated to "essl".
_UNIT_ALTERNATION = "|".join(
    re.escape(name) for name in sorted(UNITS, key=len, reverse=True)
)
_AMOUNT = re.compile(
    # The en dash in the range separator is deliberate: recipes use it as often
    # as a plain hyphen.
    rf"^\s*(?P<qty>{_QUANTITY}(?:\s*[-–]\s*{_QUANTITY})?)?"  # noqa: RUF001
    rf"\s*(?P<unit>{_UNIT_ALTERNATION})?\.?(?![\wäöüß])\s*",
    re.IGNORECASE,
)
_NOISE = re.compile(
    r"\b(frisch|gehackt|gehackte[rn]?|fein|grob|nach Belieben|optional|ca\.|etwas)\b",
    re.IGNORECASE,
)


@dataclasses.dataclass(slots=True)
class Amount:
    """The quantity part of a recipe line, kept rather than discarded."""

    value: float | None = None
    """Numeric quantity; the upper bound of a range like "2-3"."""
    unit: str | None = None
    """Canonical unit, e.g. ``"EL"``, ``"g"``, ``"Zehe"``."""
    kind: str | None = None
    """One of ``weight``, ``volume``, ``count``, ``spoon``."""
    raw: str = ""
    """The text this was parsed from, verbatim."""

    @property
    def is_spoon_scale(self) -> bool:
        """True for spoon/pinch amounts, which no pack size can satisfy."""
        return self.kind == SPOON

    def __str__(self) -> str:
        if self.value is None and not self.unit:
            return ""
        number = ""
        if self.value is not None:
            number = f"{self.value:g}" if self.value % 1 else f"{int(self.value)}"
        return f"{number} {self.unit}".strip() if self.unit else number

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "kind": self.kind,
            "raw": self.raw,
            "text": str(self),
        }


def _parse_quantity(text: str) -> float | None:
    """Turn "1½", "3/4", "2-3" or "1.5" into a number; ranges yield the upper bound."""
    if not text:
        return None
    # A range means "somewhere between"; buying for the upper bound is the safe read.
    parts = re.split(r"\s*[-–]\s*", text.strip())  # noqa: RUF001
    total = None
    for part in parts:
        value = 0.0
        seen = False
        fraction_match = re.search(f"[{_FRACTION_CHARS}]", part)
        if fraction_match:
            value += _VULGAR_FRACTIONS[fraction_match.group(0)]
            seen = True
            part = part[: fraction_match.start()]
        if "/" in part:
            num, _, den = part.partition("/")
            try:
                value += float(num.strip().replace(",", ".")) / float(den.strip())
                seen = True
            except (ValueError, ZeroDivisionError):
                pass
        elif part.strip():
            try:
                value += float(part.strip().replace(",", "."))
                seen = True
            except ValueError:
                pass
        if seen:
            total = value
    return total


def parse_ingredient(line: str) -> tuple[Amount, str]:
    """Split a recipe line into its amount and a searchable product term.

    ``"2 EL Olivenöl"`` -> ``(Amount(2, "EL", "spoon"), "Olivenöl")``
    """
    text = re.sub(r"^[-*•]\s*", "", line.strip())
    match = _AMOUNT.match(text)
    amount = Amount()
    if match:
        qty, unit = match.group("qty"), match.group("unit")
        if qty or unit:
            canonical, kind = (
                UNITS.get(unit.lower().rstrip("."), (unit, None))
                if unit
                else (None, None)
            )
            amount = Amount(
                value=_parse_quantity(qty or ""),
                unit=canonical,
                kind=kind,
                raw=match.group(0).strip(),
            )
            text = text[match.end() :]

    term = text.split(",")[0]
    term = _NOISE.sub("", term)
    return amount, re.sub(r"\s+", " ", term).strip(" .-")


def clean_ingredient(line: str) -> str:
    """Reduce a recipe line to a searchable product term.

    ``"500 g Spaghetti, fein"`` -> ``"Spaghetti"``
    """
    return parse_ingredient(line)[1]


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
    amount: Amount = dataclasses.field(default_factory=lambda: Amount())
    """How much the recipe asks for. Never used to pick a pack size - see the README."""

    @property
    def is_spoon_scale(self) -> bool:
        """Recipe needs a spoonful or a pinch, so you may already have it in."""
        return self.amount.is_spoon_scale

    def as_dict(self) -> dict:
        """Plain JSON-serialisable representation."""
        return {
            "line": self.line,
            "term": self.term,
            "amount": self.amount.as_dict(),
            "resolved": self.resolved,
            "spoon_scale": self.is_spoon_scale,
            "best": self.best.as_dict() if self.best else None,
            "alternatives": [p.as_dict() for p in self.alternatives],
            "preference": self.preference,
            "preference_applied": self.preference_applied,
            "note": self.note,
        }

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
    amount, term = parse_ingredient(line)
    if not term:
        return Match(line=line, term=term, best=None, amount=amount)
    try:
        hits, applied = _search_preferring(coop, term, candidates, filters, prefer)
    except CoopError as exc:
        return Match(
            line=line,
            term=term,
            best=None,
            note=str(exc),
            preference=prefer,
            amount=amount,
        )
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
        amount=amount,
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

"""Search filters and the preference vocabulary."""

import pytest

from grocery.stores.coop.filters import SearchFilters, normalise_preference


def test_preference_aliases_normalise():
    assert normalise_preference("Prix Garantie") == "budget"
    assert normalise_preference("organic") == "bio"
    assert normalise_preference("cheap") == "cheapest"
    assert normalise_preference(None) is None
    assert normalise_preference("none") is None


def test_unknown_preference_raises():
    with pytest.raises(ValueError, match="Unknown preference"):
        normalise_preference("fancy")


def test_as_kwargs_lists_only_active_flags():
    filters = SearchFilters(vegan=True, bio=True)
    assert filters.as_kwargs() == {"vegan": True, "bio": True}
    assert not SearchFilters().as_kwargs()


def test_checkbox_ids_cover_dietary_and_flags():
    filters = SearchFilters(vegan=True, bio=True)
    assert filters.checkbox_ids() == ["nutritionalProperties-VEGAN", "bioFacet-true"]

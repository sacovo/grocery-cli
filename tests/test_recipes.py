"""Ingredient-line parsing: the pure logic under `coop recipe`."""

from grocery.recipes import clean_ingredient, parse_ingredient, parse_recipe


def test_amount_and_term_are_split():
    amount, term = parse_ingredient("500 g Spaghetti, fein")
    assert term == "Spaghetti"
    assert amount.value == 500
    assert amount.unit == "g"
    assert amount.kind == "weight"


def test_spoon_scale_is_flagged():
    amount, term = parse_ingredient("2 EL Olivenöl")
    assert term == "Olivenöl"
    assert amount.is_spoon_scale


def test_vulgar_fractions():
    amount, _ = parse_ingredient("1½ TL Salz")
    assert amount.value == 1.5


def test_ranges_use_the_upper_bound():
    amount, term = parse_ingredient("2-3 Zehen Knoblauch")
    assert amount.value == 3
    assert amount.unit == "Zehe"
    assert term == "Knoblauch"


def test_noise_words_are_stripped():
    assert clean_ingredient("1 Bund Petersilie, frisch gehackt") == "Petersilie"


def test_line_without_amount_survives():
    amount, term = parse_ingredient("Parmesan")
    assert term == "Parmesan"
    assert amount.value is None
    assert not amount.is_spoon_scale


def test_parse_recipe_skips_blanks_and_comments():
    text = "# Carbonara\n\n500 g Spaghetti\n  \n150 g Speck\n"
    assert parse_recipe(text) == ["500 g Spaghetti", "150 g Speck"]

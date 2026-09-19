"""Arbitrary key/value rows on a car — "Colour: white", "Turbo: yes".

The nine fixed fields on `Car` cover what every car has. This covers what one car
happens to have, without a schema change per idea somebody has on a Tuesday.

A row is `{label_en, label_ja, value_en, value_ja}`, and the list's order *is* the
display order. Japanese is optional and falls back to English, mirroring
`description_en`/`description_ja` — staff write one language far more often than two,
and showing a reader nothing is worse than showing them the other one.

This module exists rather than living in the form because the form is not the only
writer: the migration importer and the tests need the same definition of what a valid
row is, and a second copy of it would drift.
"""

#: Room for a generous spec sheet and no more. The ceiling is not about storage: `Car`
#: is an AllProjection into GSI1 and every listing page reads the whole item, so each
#: pair is paid for on pages that never display it. Twenty pairs in two languages is
#: already a noticeable fraction of a car item.
MAX_PAIRS = 20
MAX_LABEL = 60
MAX_VALUE = 200

FIELDS = ("label_en", "label_ja", "value_en", "value_ja")


class TooManySpecs(ValueError):
    """Raised rather than truncating: silently dropping row 21 is a data loss bug."""


def clean(rows):
    """Normalise submitted rows into what the store should hold.

    Everything is stripped, blank-in-both-languages rows are dropped, and the result is
    `None` rather than `[]` when nothing survives -- so `attribute_not_exists(specs)`
    means "no specs", the same way `price_jpy` distinguishes absent from zero.

    A row is kept when it has at least one label *and* at least one value: a label with
    no value renders as an empty table row, which tells a reader nothing and makes the
    car look under-documented. Dropping those silently matches `CarImageForm`, where an
    untouched extra row is not an error.
    """
    kept = []
    for row in rows or []:
        pair = {name: (row.get(name) or "").strip()[:_cap(name)] for name in FIELDS}
        if not (pair["label_en"] or pair["label_ja"]):
            continue
        if not (pair["value_en"] or pair["value_ja"]):
            continue
        kept.append(pair)

    if len(kept) > MAX_PAIRS:
        raise TooManySpecs(f"A car can have at most {MAX_PAIRS} extra details.")
    return kept or None


def _cap(name):
    return MAX_LABEL if name.startswith("label") else MAX_VALUE


def localized(row, language):
    """`(label, value)` in the reader's language, each falling back independently.

    Independently on purpose: staff who translate the label but not the value are
    common, and pairing the fallbacks would then show an English label beside a
    Japanese value for no reason.
    """
    return _pick(row, "label", language), _pick(row, "value", language)


def _pick(row, part, language):
    preferred = row.get(f"{part}_ja" if language == "ja" else f"{part}_en") or ""
    fallback = row.get(f"{part}_en" if language == "ja" else f"{part}_ja") or ""
    return preferred.strip() or fallback.strip()

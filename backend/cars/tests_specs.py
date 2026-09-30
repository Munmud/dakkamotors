"""The rules for free-form car details.

`specs.py` is the single definition, shared by the staff form and the importer, so the
rules are asserted here rather than through a view -- and the view tests in
`tests_staff_cars.py` then only have to prove the form reaches them.
"""

from django.test import SimpleTestCase

from . import seo
from . import specs


class CleanTests(SimpleTestCase):
    def row(self, **fields):
        return {name: fields.get(name, "") for name in specs.FIELDS}

    def test_a_complete_row_survives(self):
        kept = specs.clean([self.row(label_en="Colour", value_en="White")])

        self.assertEqual(kept, [self.row(label_en="Colour", value_en="White")])

    def test_everything_is_stripped(self):
        kept = specs.clean([self.row(label_en="  Colour ", value_en=" White  ")])

        self.assertEqual(kept[0]["label_en"], "Colour")
        self.assertEqual(kept[0]["value_en"], "White")

    def test_a_row_with_no_label_is_dropped(self):
        self.assertIsNone(specs.clean([self.row(value_en="White")]))

    def test_a_row_with_no_value_is_dropped(self):
        self.assertIsNone(specs.clean([self.row(label_en="Colour")]))

    def test_japanese_alone_is_enough(self):
        """Staff write one language far more often than two."""
        kept = specs.clean([self.row(label_ja="色", value_ja="白")])

        self.assertEqual(len(kept), 1)

    def test_nothing_becomes_none_not_an_empty_list(self):
        """So `attribute_not_exists(specs)` means "none", as it does for `price_jpy`."""
        self.assertIsNone(specs.clean([]))
        self.assertIsNone(specs.clean(None))
        self.assertIsNone(specs.clean([self.row(), self.row()]))

    def test_order_is_preserved(self):
        kept = specs.clean([
            self.row(label_en="B", value_en="2"),
            self.row(label_en="A", value_en="1"),
        ])

        self.assertEqual([r["label_en"] for r in kept], ["B", "A"])

    def test_long_text_is_cut_to_the_cap(self):
        kept = specs.clean([self.row(label_en="x" * 500, value_en="y" * 500)])

        self.assertEqual(len(kept[0]["label_en"]), specs.MAX_LABEL)
        self.assertEqual(len(kept[0]["value_en"]), specs.MAX_VALUE)

    def test_too_many_rows_raise_rather_than_truncate(self):
        """Silently dropping row 21 is data loss wearing a validation costume."""
        rows = [self.row(label_en=str(i), value_en=str(i))
                for i in range(specs.MAX_PAIRS + 1)]

        with self.assertRaises(specs.TooManySpecs):
            specs.clean(rows)

    def test_exactly_the_cap_is_allowed(self):
        rows = [self.row(label_en=str(i), value_en=str(i))
                for i in range(specs.MAX_PAIRS)]

        self.assertEqual(len(specs.clean(rows)), specs.MAX_PAIRS)


class LocalizedTests(SimpleTestCase):
    def test_japanese_is_preferred_when_present(self):
        row = {"label_en": "Colour", "label_ja": "色",
               "value_en": "White", "value_ja": "白"}

        self.assertEqual(specs.localized(row, "ja"), ("色", "白"))
        self.assertEqual(specs.localized(row, "en"), ("Colour", "White"))

    def test_each_half_falls_back_on_its_own(self):
        """A translated label beside an untranslated value is the common case.

        Pairing the fallbacks would answer a half-translated row with the English label
        as well, throwing away a translation somebody typed.
        """
        row = {"label_en": "Colour", "label_ja": "色",
               "value_en": "Pearl White", "value_ja": ""}

        self.assertEqual(specs.localized(row, "ja"), ("色", "Pearl White"))


class MileageFromSpecsTests(SimpleTestCase):
    """`mileageFromOdometer` is read out of the free-form rows.

    There is no mileage field on `Car` -- not every car has a figure the shop will
    state -- so the one number Google's vehicle rich result wants has to be recovered
    from text staff typed. The whole risk is a wrong number appearing in a search
    result beside the car, so this reads strictly and gives up readily.
    """

    def mileage(self, value, label="Mileage"):
        return seo.mileage_from([{"label_en": label, "value_en": value}])

    def test_the_shapes_staff_actually_type(self):
        for value in ("215000km", "215,000km", "215000 km", "215000", "215,000 KM"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.mileage(value),
                    {"@type": "QuantitativeValue", "value": 215000,
                     "unitCode": "KMT"})

    def test_a_plus_is_a_floor_not_a_reading(self):
        """"215000+km" says at least, and Schema.org has a word for that.

        Emitting it as `value` would state a precision the shop did not, which is the
        one failure mode this whole reader exists to avoid.
        """
        self.assertEqual(
            self.mileage("215000+km"),
            {"@type": "QuantitativeValue", "minValue": 215000, "unitCode": "KMT"})

    def test_japanese_rows_are_read_too(self):
        self.assertEqual(
            seo.mileage_from([{"label_ja": "走行距離", "value_ja": "約88,000キロ"}]),
            {"@type": "QuantitativeValue", "value": 88000, "unitCode": "KMT"})
        self.assertEqual(
            self.mileage("215000以上")["minValue"], 215000)

    def test_anything_it_cannot_read_yields_nothing(self):
        for value in ("low", "12,000 - 15,000km", "0km", "ask us", ""):
            with self.subTest(value=value):
                self.assertIsNone(self.mileage(value))

    def test_a_number_under_another_label_is_not_mileage(self):
        """The label is what identifies the row. A price is also a number with commas."""
        self.assertIsNone(self.mileage("215000", label="Colour"))

    def test_no_specs_at_all(self):
        self.assertIsNone(seo.mileage_from(None))
        self.assertIsNone(seo.mileage_from([]))

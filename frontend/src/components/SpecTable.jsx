import { useTranslation } from "react-i18next";

import { carField, pickLocalized } from "../lib/format";

/**
 * Specification rows. Blank fields are dropped rather than shown empty — an
 * unfilled row tells the reader nothing and makes the car look under-documented.
 *
 * The nine fixed rows come first and always in the same order, so the table reads the
 * same on every car. Staff-typed rows follow in the order staff arranged them: they are
 * per-car by definition, and sorting them would throw away the only ordering anybody
 * expressed.
 */
export default function SpecTable({ car }) {
  const { t, i18n } = useTranslation();

  const language = i18n.language;
  // Fuel is a fixed vocabulary, so it is translated here rather than sent translated:
  // the reader switches language without a refetch. The display string from the API
  // is the fallback for a value this build has no word for.
  const fuel = car.fuel_type && i18n.exists(`fuel.${car.fuel_type}`)
    ? t(`fuel.${car.fuel_type}`)
    : car.fuel_type_display;

  const rows = [
    ["brand", carField(car, "brand", language)],
    ["model", carField(car, "model_name", language)],
    ["grade", car.grade],
    ["year", car.manufacture_year],
    ["fuel", fuel],
    ["seats", car.seat_capacity],
    ["color", carField(car, "color", language)],
    ["modelCode", car.model_code],
    ["chassisNumber", car.chassis_number],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");

  const numeric = new Set(["year", "seats", "chassisNumber", "modelCode"]);

  /* The label is already the reader's words, so it is printed rather than passed to
     `t()` — there is no key for "Tow bar" and there never will be. */
  const extra = (car.specs ?? [])
    .map((row, index) => ({
      id: `spec-${index}`,
      label: pickLocalized(row, "label", i18n.language),
      value: pickLocalized(row, "value", i18n.language),
    }))
    .filter((row) => row.label && row.value);

  return (
    <dl className="specs">
      {rows.map(([key, value]) => (
        <div key={key} style={{ display: "contents" }}>
          <dt className="specs__key">{t(`spec.${key}`)}</dt>
          <dd className={numeric.has(key) ? "specs__value u-nums" : "specs__value"}>
            {value}
          </dd>
        </div>
      ))}
      {extra.map((row) => (
        <div key={row.id} style={{ display: "contents" }}>
          <dt className="specs__key">{row.label}</dt>
          <dd className="specs__value">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

import { useTranslation } from "react-i18next";

import { pickLocalized } from "../lib/format";

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

  const rows = [
    ["brand", car.brand],
    ["model", car.model_name],
    ["grade", car.grade],
    ["year", car.manufacture_year],
    ["fuel", car.fuel_type_display],
    ["seats", car.seat_capacity],
    ["color", car.color],
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

import { useTranslation } from "react-i18next";

/**
 * Specification rows. Blank fields are dropped rather than shown empty — an
 * unfilled row tells the reader nothing and makes the car look under-documented.
 */
export default function SpecTable({ car }) {
  const { t } = useTranslation();

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
    </dl>
  );
}

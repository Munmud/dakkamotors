/** Display and formatting helpers shared by the list and detail pages. */

const RAW_PHONE = import.meta.env.VITE_CONTACT_PHONE ?? "";

/** Human-readable number, exactly as configured. */
export const phoneDisplay = RAW_PHONE.trim();

/**
 * tel: target in E.164. A Japanese number written domestically starts with a trunk
 * "0" that must be dropped when the +81 country code is added, otherwise the call
 * fails for anyone dialling from outside Japan.
 */
export const phoneHref = (() => {
  const digits = phoneDisplay.replace(/[^\d+]/g, "");
  if (!digits) return "";
  if (digits.startsWith("+")) return digits;
  if (digits.startsWith("0")) return `+81${digits.slice(1)}`;
  return `+${digits}`;
})();

export const hasPhone = phoneHref.length > 0;

/** ¥ amount with thousands separators, or null when the price is "call for price". */
export function formatPrice(amount, locale) {
  if (amount === null || amount === undefined) return null;
  return new Intl.NumberFormat(locale === "ja" ? "ja-JP" : "en-US").format(amount);
}

/** Full name of a car as one string: "2018 Daihatsu Tanto". */
/**
 * A car field with an optional Japanese twin: `brand` and `brand_ja`, `color` and
 * `color_ja`. Unlike the `_en`/`_ja` pairs on specs, the bare field *is* the English
 * -- it is the identifier the slug and the JSON-LD are built from -- so an English
 * reader never sees the Japanese, and a Japanese reader sees it only when staff typed
 * one. Falls back field by field: a Japanese make with no Japanese model reads
 * "ダイハツ Tanto" rather than losing the model.
 */
export function carField(car, base, language) {
  const ja = (car?.[`${base}_ja`] || "").trim();
  return (language === "ja" && ja) || (car?.[base] ?? "");
}

/** Year, make and model in the reader's language. */
export function carName(car, language) {
  return [
    car.manufacture_year,
    carField(car, "brand", language),
    carField(car, "model_name", language),
  ].filter(Boolean).join(" ");
}

export function carTitle(car, language = "en") {
  return carName(car, language);
}

/**
 * One `_en`/`_ja` pair in the reader's language, falling back to the other.
 *
 * `pickDescription` is the same idea bound to one field name. This is the general form,
 * and free-form specs need it twice per row — once for the label and once for the value,
 * resolved independently: staff who translate the label but not the value are common,
 * and pairing the fallbacks would then show an English label beside a Japanese value.
 */
export function pickLocalized(source, base, language) {
  const preferred = source?.[language === "ja" ? `${base}_ja` : `${base}_en`];
  const fallback = source?.[language === "ja" ? `${base}_en` : `${base}_ja`];
  return (preferred || "").trim() || (fallback || "").trim() || "";
}

/**
 * Description in the reader's language, falling back to whichever field is filled.
 * Staff often write only one language; showing nothing would be worse than showing
 * the other one.
 */
export function pickDescription(car, language) {
  const preferred = language === "ja" ? car.description_ja : car.description_en;
  const fallback = language === "ja" ? car.description_en : car.description_ja;
  return (preferred || "").trim() || (fallback || "").trim() || "";
}

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
export function carTitle(car) {
  return [car.manufacture_year, car.brand, car.model_name].filter(Boolean).join(" ");
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

/**
 * Slot times are always shown in Japan time, whatever timezone the visitor is in.
 *
 * The API sends UTC. A buyer abroad browsing at 3am their time still needs to see the
 * appointment as the shop will keep it, so the timezone is pinned rather than taken
 * from the browser.
 */

const TIMEZONE = "Asia/Tokyo";

function locale(language) {
  return language === "ja" ? "ja-JP" : "en-GB";
}

export function formatSlotDay(iso, language) {
  return new Intl.DateTimeFormat(locale(language), {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: TIMEZONE,
  }).format(new Date(iso));
}

export function formatSlotTime(iso, language) {
  return new Intl.DateTimeFormat(locale(language), {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: TIMEZONE,
  }).format(new Date(iso));
}

export function formatSlotFull(iso, language) {
  return `${formatSlotDay(iso, language)}, ${formatSlotTime(iso, language)}`;
}

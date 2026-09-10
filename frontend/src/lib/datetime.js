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

/** "Sat" / "土" — the weekday alone, for a compact date tile. */
export function formatSlotWeekday(iso, language) {
  return new Intl.DateTimeFormat(locale(language), {
    weekday: "short",
    timeZone: TIMEZONE,
  }).format(new Date(iso));
}

/** "13" — the day of the month alone, so the tile can size it like a number. */
export function formatSlotDayNumber(iso, language) {
  return new Intl.DateTimeFormat(locale(language), {
    day: "numeric",
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

/**
 * A stable "YYYY-MM-DD" for the shop's calendar day, whatever timezone the visitor is in.
 *
 * This is an identity, not a label — the picker keys its selected date on it. That rules
 * out `formatSlotDay`, whose output changes the moment someone uses the language switch,
 * which would strand a half-made selection on a key that no longer exists.
 *
 * Built with formatToParts rather than the widespread `en-CA gives you ISO` trick, which
 * is an observation about current implementations rather than a guarantee.
 */
export function tokyoDateKey(iso) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(iso));
  const part = (type) => parts.find((p) => p.type === type).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/**
 * Slots bucketed into the shop's days: [{ key, iso, slots }], earliest first.
 *
 * Relies on the API returning slots in ascending order, which `bookable_slots()` does.
 * A day with no bookable slot cannot appear here, because the server has already dropped
 * anything past, too soon, closed or full — so the picker never has to grey a date out.
 */
export function groupSlotsByTokyoDate(slots) {
  const days = new Map();
  for (const slot of slots) {
    const key = tokyoDateKey(slot.starts_at);
    if (!days.has(key)) days.set(key, { key, iso: slot.starts_at, slots: [] });
    days.get(key).slots.push(slot);
  }
  return [...days.values()];
}

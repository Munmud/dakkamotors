import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  dateKey,
  formatMonthLabel,
  formatSlotDay,
  formatSlotTime,
  groupSlotsByTokyoDate,
  monthCells,
  weekdayInitials,
} from "../lib/datetime";

/**
 * Pick a date on the calendar, then a time on that date.
 *
 * The calendar stays on screen once a date is chosen, so changing your mind is one click
 * rather than a trip back through a separate step.
 *
 * Only dates the server offered are selectable. It has already dropped anything past, too
 * soon, closed or full, so a date with no bookable time cannot be picked at all — the
 * greyed-out days are genuinely unavailable rather than merely undecided.
 */
export default function SlotPicker({ slots, selected, onSelect, disabled = false }) {
  const { t, i18n } = useTranslation();
  const days = useMemo(() => groupSlotsByTokyoDate(slots), [slots]);
  const byKey = useMemo(() => new Map(days.map((day) => [day.key, day])), [days]);

  const [chosenDate, setChosenDate] = useState(null);

  // Derived, never stored: if someone takes the last place on the chosen date while this
  // page is open, the refreshed slots prop drops that day and the times disappear in the
  // same render. Resetting from an effect would paint one frame of an empty list first.
  const day = chosenDate ? byKey.get(chosenDate) : null;
  const dateVanished = Boolean(chosenDate) && !day;

  const bounds = useMemo(() => {
    if (!days.length) return null;
    const first = days[0].key.split("-").map(Number);
    const last = days[days.length - 1].key.split("-").map(Number);
    return {
      first: { year: first[0], month: first[1] - 1 },
      last: { year: last[0], month: last[1] - 1 },
    };
  }, [days]);

  // Derived rather than initialised in an effect: until someone pages the calendar, the
  // month on show is simply the one holding the first free date. Opening there rather
  // than on today matters when the shop is booked solid until next month — an empty grid
  // is the wrong first impression.
  const [paged, setPaged] = useState(null);
  const view = paged ?? bounds?.first ?? null;

  if (!slots.length) {
    return <p className="state__body">{t("booking.noSlots")}</p>;
  }
  if (!view) return null;

  const asIndex = (month) => month.year * 12 + month.month;
  const canGoBack = asIndex(view) > asIndex(bounds.first);
  const canGoForward = asIndex(view) < asIndex(bounds.last);

  const step = (delta) => {
    const moved = view.month + delta;
    setPaged({
      year: view.year + Math.floor(moved / 12),
      month: ((moved % 12) + 12) % 12,
    });
  };

  return (
    <div className="slotpicker">
      {dateVanished && (
        <p className="authform__error" role="alert">
          {t("booking.dateGone")}
        </p>
      )}

      <h2 className="slotpicker__step">{t("booking.chooseDate")}</h2>

      <div className="cal">
        <div className="cal__bar">
          <button
            type="button"
            className="cal__nav"
            onClick={() => step(-1)}
            disabled={disabled || !canGoBack}
            aria-label={t("booking.previousMonth")}
          >
            ‹
          </button>
          <span className="cal__month" aria-live="polite">
            {formatMonthLabel(view.year, view.month, i18n.language)}
          </span>
          <button
            type="button"
            className="cal__nav"
            onClick={() => step(1)}
            disabled={disabled || !canGoForward}
            aria-label={t("booking.nextMonth")}
          >
            ›
          </button>
        </div>

        <div className="cal__grid">
          {weekdayInitials(i18n.language).map((name) => (
            <span className="cal__weekday" key={name}>
              {name}
            </span>
          ))}

          {monthCells(view.year, view.month).map((dayNumber, position) => {
            if (dayNumber === null) {
              return <span className="cal__blank" key={`blank-${position}`} />;
            }

            const key = dateKey(view.year, view.month, dayNumber);
            const available = byKey.get(key);

            if (!available) {
              return (
                <span className="cal__day cal__day--off u-nums" key={key}>
                  {dayNumber}
                </span>
              );
            }

            return (
              <button
                type="button"
                key={key}
                className="cal__day cal__day--open u-nums"
                aria-pressed={chosenDate === key}
                disabled={disabled}
                aria-label={`${formatSlotDay(available.iso, i18n.language)}, ${t(
                  "booking.timesAvailable",
                  { count: available.slots.length },
                )}`}
                onClick={() => {
                  setChosenDate(key);
                  // A time picked on the previous date is no longer what they mean.
                  onSelect(null);
                }}
              >
                {dayNumber}
              </button>
            );
          })}
        </div>
      </div>

      {day && (
        <>
          <h2 className="slotpicker__step">{formatSlotDay(day.iso, i18n.language)}</h2>
          <ul className="slotpicker__times">
            {day.slots.map((slot) => (
              <li key={slot.id}>
                <button
                  type="button"
                  className="slotpicker__slot"
                  aria-pressed={selected === slot.id}
                  disabled={disabled}
                  onClick={() => onSelect(slot.id)}
                >
                  <span className="u-nums">
                    {formatSlotTime(slot.starts_at, i18n.language)}–
                    {formatSlotTime(slot.ends_at, i18n.language)}
                  </span>
                  <span className="slotpicker__left">
                    {t("booking.seatsLeft", { count: slot.seats_left })}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

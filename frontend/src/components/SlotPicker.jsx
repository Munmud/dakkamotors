import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  formatSlotDay,
  formatSlotDayNumber,
  formatSlotTime,
  formatSlotWeekday,
  groupSlotsByTokyoDate,
} from "../lib/datetime";

/**
 * Pick a date, then a time.
 *
 * One list of every slot across the booking horizon runs to a couple of hundred buttons,
 * which is a wall rather than a choice. Splitting it means a visitor answers the question
 * they already know the answer to — which day suits them — before being shown anything
 * else.
 *
 * Only shows what the API offered: the server has already excluded anything past, too
 * soon, closed or full, and re-checks all of it on submit. A consequence worth knowing is
 * that a date with no bookable time cannot appear here at all, so nothing is ever greyed
 * out — today simply drops off the front of the list once its last slot has passed.
 */
export default function SlotPicker({ slots, selected, onSelect, disabled = false }) {
  const { t, i18n } = useTranslation();
  const [chosenDate, setChosenDate] = useState(null);
  const backRef = useRef(null);

  const days = useMemo(() => groupSlotsByTokyoDate(slots), [slots]);

  // Derived, never stored. If someone takes the last place on the chosen date while this
  // page is open, the refreshed `slots` prop drops that day and this falls back to the
  // date step in the same render. Resetting from an effect would paint one frame of an
  // empty time list first.
  const day = chosenDate ? days.find((entry) => entry.key === chosenDate) : null;
  const dateVanished = Boolean(chosenDate) && !day;

  useEffect(() => {
    // The list under the heading has just been replaced; say so by moving the caret
    // there, rather than leaving a screen reader to discover it.
    if (day) backRef.current?.focus();
  }, [day]);

  if (!slots.length) {
    return <p className="state__body">{t("booking.noSlots")}</p>;
  }

  if (!day) {
    return (
      <div className="slotpicker">
        {dateVanished && (
          <p className="authform__error" role="alert">
            {t("booking.dateGone")}
          </p>
        )}
        <h2 className="slotpicker__step">{t("booking.chooseDate")}</h2>
        <ul className="slotpicker__dates">
          {days.map((entry) => (
            <li key={entry.key}>
              <button
                type="button"
                className="slotpicker__date"
                disabled={disabled}
                onClick={() => setChosenDate(entry.key)}
              >
                <span className="slotpicker__weekday">
                  {formatSlotWeekday(entry.iso, i18n.language)}
                </span>
                <span className="slotpicker__daynum u-nums">
                  {formatSlotDayNumber(entry.iso, i18n.language)}
                </span>
                <span className="slotpicker__count">
                  {t("booking.timesAvailable", { count: entry.slots.length })}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <div className="slotpicker">
      <button
        type="button"
        className="slotpicker__back"
        ref={backRef}
        disabled={disabled}
        onClick={() => {
          setChosenDate(null);
          onSelect(null);
        }}
      >
        {t("booking.changeDate")}
      </button>
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
    </div>
  );
}

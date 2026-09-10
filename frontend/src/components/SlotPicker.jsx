import { useTranslation } from "react-i18next";

import { formatSlotDay, formatSlotTime } from "../lib/datetime";

/**
 * Available times, grouped by day.
 *
 * Only shows what the API offered — the server has already excluded anything past, too
 * soon, closed or full, and re-checks all of it on submit. Nothing here is a guarantee;
 * a slot can fill between rendering and clicking, which is why booking can still fail
 * with a message.
 */
export default function SlotPicker({ slots, selected, onSelect, disabled = false }) {
  const { t, i18n } = useTranslation();

  if (!slots.length) {
    return <p className="state__body">{t("booking.noSlots")}</p>;
  }

  const byDay = slots.reduce((groups, slot) => {
    const key = formatSlotDay(slot.starts_at, i18n.language);
    (groups[key] ||= []).push(slot);
    return groups;
  }, {});

  return (
    <div className="slotpicker">
      {Object.entries(byDay).map(([day, daySlots]) => (
        <div className="slotpicker__day" key={day}>
          <h3 className="slotpicker__date">{day}</h3>
          <ul className="slotpicker__times">
            {daySlots.map((slot) => (
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
      ))}
    </div>
  );
}

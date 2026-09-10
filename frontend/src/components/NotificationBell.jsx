import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { formatSlotFull } from "../lib/datetime";
import { useNotifications } from "../lib/NotificationContext";

function BellIcon() {
  return (
    <svg className="bell__icon" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
      <path d="M12 22a2.2 2.2 0 0 0 2.2-2.2H9.8A2.2 2.2 0 0 0 12 22Zm6.6-5.5v-5a6.7 6.7 0 0 0-5-6.6V4a1.6 1.6 0 0 0-3.2 0v.9a6.7 6.7 0 0 0-5 6.6v5L3.5 18v.9h17V18Z" />
    </svg>
  );
}

/**
 * One line per notification.
 *
 * The words are produced here from `kind` and `context`, never stored — so someone who
 * signs up in English and later switches to Japanese sees their whole history in
 * Japanese, rather than a feed frozen in the language they happened to register in.
 *
 * An unrecognised kind falls back to a generic line rather than throwing: the backend and
 * the frontend deploy from separate workflows with separate path filters, so a new kind
 * can reach a browser still running the previous bundle, and an exception here would take
 * out the masthead on every page of the site.
 */
function describe(item, t, language) {
  const context = item.context ?? {};
  const known = ["booking_confirmed", "booking_cancelled", "question_answered"];
  const key = known.includes(item.kind) ? item.kind : "generic";
  const when = context.starts_at ? formatSlotFull(context.starts_at, language) : "";
  return t(`notifications.${key}`, { car: context.car_label || "", when });
}

function target(item) {
  const context = item.context ?? {};
  if (item.kind === "question_answered" && context.car_slug) {
    return `/cars/${context.car_slug}`;
  }
  return "/account";
}

export default function NotificationBell() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { items, unread, state, markAllRead } = useNotifications();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);
  const buttonRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    const onClick = (event) => {
      if (!wrapRef.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  // Nothing for anonymous visitors, and nothing during the session check — the same
  // reasoning as AccountLink, so a signed-in visitor never sees the wrong thing first.
  if (state !== "loaded") return null;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && unread > 0) markAllRead();
  }

  return (
    <div className="bell" ref={wrapRef}>
      <button
        type="button"
        className="bell__button"
        ref={buttonRef}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={t("notifications.bell", { count: unread })}
        onClick={toggle}
      >
        <BellIcon />
        {unread > 0 && <span className="bell__count u-nums">{unread}</span>}
      </button>

      {open && (
        <div className="bell__panel">
          <p className="bell__title">{t("notifications.title")}</p>
          {items.length === 0 ? (
            <p className="bell__empty">{t("notifications.empty")}</p>
          ) : (
            <ul className="bell__list">
              {items.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    className="bell__item"
                    onClick={() => {
                      setOpen(false);
                      navigate(target(item));
                    }}
                  >
                    {describe(item, t, i18n.language)}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

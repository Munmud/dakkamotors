import { useTranslation } from "react-i18next";

import { hasPhone, phoneDisplay, phoneHref } from "../lib/format";

function ReceiverIcon() {
  return (
    <svg className="callbtn__icon" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
      <path d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25 11.4 11.4 0 0 0 3.6.58 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.25.2 2.46.57 3.6a1 1 0 0 1-.25 1z" />
    </svg>
  );
}

/**
 * Primary conversion on the whole site. Renders nothing when no number is
 * configured — a dead call button is worse than none.
 */
export default function CallButton({ onInk = false }) {
  const { t } = useTranslation();

  if (!hasPhone) return null;

  return (
    <a
      className={onInk ? "callbtn callbtn--onink" : "callbtn"}
      href={`tel:${phoneHref}`}
    >
      <ReceiverIcon />
      <span className="u-nums">{t("call.action", { phone: phoneDisplay })}</span>
    </a>
  );
}

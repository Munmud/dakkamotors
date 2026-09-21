import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import NotificationBell from "./NotificationBell";

import { LANGUAGES } from "../i18n";
import { useAuth } from "../lib/AuthContext";

function LanguageSwitch() {
  const { t, i18n } = useTranslation();

  return (
    <div className="langswitch" role="group" aria-label={t("language.label")}>
      {LANGUAGES.map((code) => (
        <button
          key={code}
          type="button"
          className="langswitch__option"
          aria-pressed={i18n.language === code}
          onClick={() => i18n.changeLanguage(code)}
        >
          {t(`language.${code}`)}
        </button>
      ))}
    </div>
  );
}

function AccountLink() {
  const { t } = useTranslation();
  const { customer, state } = useAuth();
  const { pathname } = useLocation();
  // Nothing until the session check finishes, so a signed-in visitor never sees
  // "Sign in" flash first.
  if (state === "unknown") return null;
  if (customer) {
    return (
      <Link className="masthead__account" to="/account">
        {t("booking.myBookings")}
      </Link>
    );
  }
  // Both ways in, on every page. Most people arriving here have no account yet, and a
  // lone "Sign in" reads as a door for members. `next` is the page they are on, so a
  // customer who signs in from a car comes back to that car -- except from the
  // account pages themselves, which would only loop.
  const next = pathname.startsWith("/account") ? "" : `?next=${encodeURIComponent(pathname)}`;
  return (
    <>
      <Link className="masthead__account" to={`/account/login${next}`}>
        {t("auth.signIn")}
      </Link>
      <Link className="masthead__account masthead__account--primary" to={`/account/register${next}`}>
        {t("auth.signUp")}
      </Link>
    </>
  );
}


/**
 * The way into the admin, for the people who have one.
 *
 * A plain anchor, not a router Link: /api/staff/ is rendered by Django, so React must
 * hand the browser over rather than try to route it.
 *
 * This replaced the "Staff login" link that used to sit in the footer of every page.
 * Advertising the admin URL to every visitor bought nothing - staff know where it is,
 * and anyone else reading it was not the audience. Signing in there sets the same
 * session cookie the app reads, so `is_staff` comes back from /api/auth/me/ and the
 * button appears on its own.
 */
function AdminLink() {
  const { t } = useTranslation();
  const { customer, state } = useAuth();

  if (state !== "signed-in" || !customer?.is_staff) return null;

  return (
    <a className="masthead__admin" href="/api/staff/">
      {t("nav.admin")}
    </a>
  );
}

/**
 * The DM monogram, inline rather than an <img>.
 *
 * It has to be inline for the chrome to survive: the gradient is the mark, and an
 * <img> would need its own file fetched before the masthead could finish painting.
 * The wordmark beside it stays real text, so it is selectable, translatable and
 * carries the accessible name -- which is why the svg itself is aria-hidden rather
 * than labelled, and the link reads as "Dakka Motors" once, not twice.
 *
 * The gradient id is namespaced because ids in inline SVG are document-global, and
 * `url(#...)` binds to whichever element got there first.
 */
function BrandMark() {
  return (
    <svg className="brandmark__icon" viewBox="0 0 98 44" aria-hidden="true"
         focusable="false">
      <defs>
        <linearGradient id="brandmark-chrome" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#e8edf2" />
          <stop offset="0.18" stopColor="#ffffff" />
          <stop offset="0.38" stopColor="#b0bcc8" />
          <stop offset="0.52" stopColor="#f2f6f9" />
          <stop offset="0.72" stopColor="#96a3b0" />
          <stop offset="1" stopColor="#dde4ea" />
        </linearGradient>
      </defs>
      <g fill="url(#brandmark-chrome)">
        <path fillRule="evenodd"
              d="M0 0 H30 Q46 0 46 16 V28 Q46 44 30 44 H0 Z
                 M12 12 H27 Q34 12 34 19 V25 Q34 32 27 32 H12 Z" />
        <path transform="translate(52 0)"
              d="M0 44 V0 H11 L23 21 L35 0 H46 V44 H35 V17 L23 38 L11 17 V44 Z" />
      </g>
    </svg>
  );
}

export default function Header() {
  const { t } = useTranslation();

  return (
    <header className="masthead">
      <div className="masthead__bar">
        <Link className="brandmark" to="/">
          <BrandMark />
          <span className="brandmark__word">{t("brand")}</span>
        </Link>
        <div className="masthead__tools">
          {/* The monogram already goes home, but a monogram is a logo, not a word:
              a first-time visitor on a car page cannot be expected to know that. */}
          <Link className="masthead__account masthead__home" to="/">
            {t("nav.home")}
          </Link>
          <NotificationBell />
          <AdminLink />
          <AccountLink />
          <LanguageSwitch />
        </div>
      </div>
    </header>
  );
}

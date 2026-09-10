import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import NotificationBell from "./NotificationBell";

import { LANGUAGES } from "../i18n";
import { useAuth } from "../lib/AuthContext";
import CallButton from "./CallButton";

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
  // Nothing until the session check finishes, so a signed-in visitor never sees
  // "Sign in" flash first.
  if (state === "unknown") return null;
  return (
    <Link className="masthead__account" to="/account">
      {customer ? t("booking.myBookings") : t("auth.signIn")}
    </Link>
  );
}


/**
 * The way into the admin, for the people who have one.
 *
 * A plain anchor, not a router Link: /api/admin/ is rendered by Django, so React must
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
    <a className="masthead__admin" href="/api/admin/">
      {t("nav.admin")}
    </a>
  );
}

export default function Header() {
  const { t } = useTranslation();
  const isHome = useLocation().pathname === "/";

  return (
    <header className="masthead">
      <div className="masthead__bar">
        <Link className="plate" to="/">
          <span className="plate__mark">D</span>
          <span>{t("brand")}</span>
        </Link>
        <div className="masthead__tools">
          <NotificationBell />
          <AdminLink />
          <AccountLink />
          <LanguageSwitch />
        </div>
      </div>

      {isHome && (
        <div className="masthead__intro">
          <p className="masthead__tagline">{t("tagline")}</p>
          <CallButton onInk />
        </div>
      )}
    </header>
  );
}

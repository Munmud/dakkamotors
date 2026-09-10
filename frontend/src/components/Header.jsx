import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

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

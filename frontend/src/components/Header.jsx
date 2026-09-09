import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { LANGUAGES } from "../i18n";
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
        <LanguageSwitch />
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

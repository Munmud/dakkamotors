import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import CallButton from "./CallButton";
import { LANGUAGES } from "../i18n";

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
          {/* The number, in the masthead, on every page. Somebody who is ready to
              speak to a person should never have to go looking: this is the shortest
              path there is from an advertisement to a conversation, and on a phone it
              dials. */}
          <CallButton compact />
          <LanguageSwitch />
        </div>
      </div>
    </header>
  );
}

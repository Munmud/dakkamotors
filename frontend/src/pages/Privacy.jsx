import { Trans, useTranslation } from "react-i18next";

import { phoneDisplay, phoneHref } from "../lib/format";

/**
 * What the site does with a visitor's information.
 *
 * It exists because the site runs a Meta pixel: Meta's business tools terms require a
 * page saying so before one may run, and it is the disclosure the APPI asks for. It is
 * written for a customer rather than for a regulator, though -- plain sentences, no
 * defined terms, and the one thing anybody actually wants to know (Meta is told which
 * pages you looked at; it is never told your name or your number) is in the second
 * section rather than the ninth.
 *
 * There is no cookie banner. A banner is for a site with a dozen trackers to negotiate
 * over; this has one, the page names two ways to turn it off, and a modal over the car
 * somebody came to look at costs more than it buys.
 *
 * Every heading and paragraph is spelled out as a literal `t("privacy.x")` rather than
 * built from a loop, for the reason the footer in App.jsx gives: `check-i18n` greps the
 * source for each key, and a template literal would force the whole `privacy` prefix
 * into its dynamic exemption and stop any of this being checked.
 */
export default function Privacy() {
  const { t } = useTranslation();

  return (
    <article className="legal">
      <h1 className="section__title">{t("privacy.title")}</h1>
      <p className="legal__meta u-nums">{t("privacy.updated")}</p>
      <p className="legal__body">{t("privacy.intro")}</p>

      <section className="legal__section">
        <h2 className="legal__head">{t("privacy.collectTitle")}</h2>
        <p className="legal__body">{t("privacy.collectBody")}</p>
      </section>

      <section className="legal__section">
        <h2 className="legal__head">{t("privacy.adsTitle")}</h2>
        <p className="legal__body">{t("privacy.adsBody")}</p>
        <p className="legal__body">{t("privacy.adsBody2")}</p>
        <p className="legal__body">{t("privacy.adsOptOut")}</p>
      </section>

      <section className="legal__section">
        <h2 className="legal__head">{t("privacy.cookiesTitle")}</h2>
        <p className="legal__body">{t("privacy.cookiesBody")}</p>
      </section>

      <section className="legal__section">
        <h2 className="legal__head">{t("privacy.storageTitle")}</h2>
        <p className="legal__body">{t("privacy.storageBody")}</p>
      </section>

      <section className="legal__section">
        <h2 className="legal__head">{t("privacy.contactTitle")}</h2>
        {/* The phone is the shop's one published contact route, so it is the one
            offered here -- but as a link in the sentence, not the yellow Call button.
            The plate colour says "this is the one action on this screen", and on this
            screen it is not: the footer's call button is a few hundred pixels below,
            and two yellow buttons on one page is how the booking flow ended up with
            three of them leading nowhere. */}
        <p className="legal__body">
          <Trans
            i18nKey="privacy.contactBody"
            values={{ phone: phoneDisplay }}
            components={{ 1: <a className="legal__tel u-nums" href={`tel:${phoneHref}`} /> }}
          />
        </p>
      </section>
    </article>
  );
}

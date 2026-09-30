import { useTranslation } from "react-i18next";

import LeadForm from "../components/LeadForm";
import { usePageTitle } from "../lib/title";

/**
 * Ask the shop to find a car it does not have.
 *
 * The same form the car pages carry, with the message required instead of optional —
 * because here the message *is* the lead. On a car page the car says what somebody
 * wants; with no car, "white Tanto, both sliding doors, under 900,000" is the only
 * thing there is to act on.
 *
 * One box for it, in the person's own words, rather than five fields for make, model,
 * year, budget and colour. The owner reuses these in social posts, and a sentence
 * reads better there than five answers stitched back together — and five fields is
 * four more reasons to give up.
 */
export default function RequestCar() {
  const { t, i18n } = useTranslation();
  usePageTitle(t("request.title"), i18n.language);

  return (
    <section className="request">
      <h1 className="section__title">{t("request.title")}</h1>
      <p className="request__intro">{t("request.intro")}</p>
      <LeadForm wish />
    </section>
  );
}

import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import VerifyCode from "../components/VerifyCode";

/**
 * /account/verify -- the code screen on its own, for somebody who closed the tab.
 *
 * The register form shows the same screen straight after sign-up, in place. This
 * route exists for the person who comes back later with the email open: they give
 * the address they signed up with and type the code, and land wherever `next` says.
 * With `?email=` in the URL the address step is skipped.
 */
export default function VerifyEmail() {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const [email, setEmail] = useState(params.get("email") || "");
  const [confirmed, setConfirmed] = useState(Boolean(params.get("email")));
  const next = params.get("next") || "/account";

  if (confirmed && email) return <VerifyCode email={email.trim().toLowerCase()} next={next} />;

  return (
    <section className="authcard">
      <h1 className="section__title">{t("auth.checkEmail")}</h1>
      <form
        className="authform"
        onSubmit={(event) => {
          event.preventDefault();
          setConfirmed(true);
        }}
      >
        <label className="authform__field">
          <span>{t("auth.enterEmail")}</span>
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
            autoComplete="email"
          />
        </label>
        <button type="submit" className="btn">{t("auth.verify")}</button>
      </form>
    </section>
  );
}

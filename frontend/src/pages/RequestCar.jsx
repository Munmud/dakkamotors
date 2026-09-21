import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { errorMessage, requestCar } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";

/**
 * Ask the shop to find a car.
 *
 * Open to anyone: the person looking for a car they cannot see on the lot is exactly
 * the person who has no account yet. A guest gives a name, an address and a number
 * so there is a way to reply; a signed-in customer gives only the wish, and the
 * server copies their details from the account -- asking them again would be asking
 * for what we already have.
 *
 * One box for the car. The owner reuses these in social posts, and a paragraph in a
 * person's own words ("white Tanto, both sliding doors, under 900,000") reads
 * better there than five fields stitched back together.
 */
export default function RequestCar() {
  const { t, i18n } = useTranslation();
  const { customer, state } = useAuth();
  const [values, setValues] = useState({ name: "", email: "", phone: "", details: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [done, setDone] = useState(false);

  const set = (field) => (event) => {
    setError(null);
    setValues((current) => ({ ...current, [field]: event.target.value }));
  };

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await requestCar({ ...values, language: i18n.language });
      setDone(true);
    } catch (err) {
      setError(errorMessage(err, t("request.failed")));
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <section className="state">
        <h1 className="state__title">{t("request.doneTitle")}</h1>
        <p className="state__body">{t("request.doneBody")}</p>
        <Link className="btn" to="/">{t("request.backToLot")}</Link>
      </section>
    );
  }

  const guest = state !== "unknown" && !customer;

  return (
    <section className="request">
      <Link className="backlink" to="/">{t("nav.back")}</Link>
      <h1 className="section__title">{t("request.title")}</h1>
      <p className="request__intro">{t("request.intro")}</p>

      <form className="authform request__form" onSubmit={submit}>
        <label className="authform__field">
          <span>{t("request.details")}</span>
          <textarea
            className="request__details"
            rows={6}
            maxLength={2000}
            value={values.details}
            onChange={set("details")}
            placeholder={t("request.placeholder")}
            required
          />
        </label>

        {guest && (
          <>
            <label className="authform__field">
              <span>{t("request.name")}</span>
              <input value={values.name} onChange={set("name")} required autoComplete="name" />
            </label>
            <label className="authform__field">
              <span>{t("request.email")}</span>
              <input
                type="email"
                value={values.email}
                onChange={set("email")}
                required
                autoComplete="email"
              />
            </label>
            <label className="authform__field">
              <span>{t("request.phone")}</span>
              <input
                type="tel"
                value={values.phone}
                onChange={set("phone")}
                required
                autoComplete="tel"
              />
            </label>
            <p className="request__aside">
              {t("request.haveAccount")}{" "}
              <Link to="/account/login?next=%2Frequest-a-car">{t("request.signIn")}</Link>
            </p>
          </>
        )}

        {customer && (
          <p className="request__aside">{t("request.replyTo", { email: customer.email })}</p>
        )}

        {error && <p className="authform__error" role="alert">{error}</p>}

        <button
          type="submit"
          className="callbtn bookbtn"
          disabled={busy || state === "unknown" || !values.details.trim()}
        >
          {busy ? t("request.sending") : t("request.submit")}
        </button>
      </form>
    </section>
  );
}

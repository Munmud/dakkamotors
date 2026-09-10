import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { confirmPasswordReset, errorMessage, requestPasswordReset } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";

/**
 * Two screens behind one route.
 *
 * With a uid+token in the query string it sets a new password; without, it asks for the
 * address to send a link to. Same as VerifyEmail, the token is read client-side because
 * the CDN strips query strings it does not cache on.
 */
export default function ResetPassword() {
  const params = new URLSearchParams(window.location.search);
  const uid = params.get("uid");
  const token = params.get("token");

  return uid && token ? <ChooseNewPassword uid={uid} token={token} /> : <AskForLink />;
}

function AskForLink() {
  const { t, i18n } = useTranslation();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await requestPasswordReset(email, i18n.language);
    } finally {
      // Always the same outcome, whether or not that address has an account - the
      // screen must not become a way to find out who is registered.
      setSent(true);
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <section className="authcard">
        <h1 className="section__title">{t("auth.checkEmail")}</h1>
        <p className="state__body">{t("auth.resetSent")}</p>
        <p className="authform__switch">
          <Link to="/account/login">{t("auth.backToSignIn")}</Link>
        </p>
      </section>
    );
  }

  return (
    <section className="authcard">
      <h1 className="section__title">{t("auth.resetTitle")}</h1>
      <p className="state__body">{t("auth.resetIntro")}</p>
      <form className="authform" onSubmit={submit}>
        <label className="authform__field">
          <span>{t("auth.email")}</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoComplete="email"
          />
        </label>
        <button type="submit" className="btn" disabled={busy}>
          {t("auth.resetTitle")}
        </button>
      </form>
      <p className="authform__switch">
        <Link to="/account/login">{t("auth.backToSignIn")}</Link>
      </p>
    </section>
  );
}

function ChooseNewPassword({ uid, token }) {
  const { t } = useTranslation();
  const { refresh } = useAuth();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await confirmPasswordReset({ uid, token, password });
      await refresh();
      navigate("/account", { replace: true });
    } catch (err) {
      setError(errorMessage(err, t("auth.verifyFailed")));
      setBusy(false);
    }
  }

  return (
    <section className="authcard">
      <h1 className="section__title">{t("auth.resetTitle")}</h1>
      <form className="authform" onSubmit={submit}>
        <label className="authform__field">
          <span>{t("auth.newPassword")}</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete="new-password"
          />
        </label>
        {error && <p className="authform__error" role="alert">{error}</p>}
        <button type="submit" className="btn" disabled={busy}>
          {t("auth.setPassword")}
        </button>
      </form>
      <p className="authform__switch">
        <Link to="/account/login">{t("auth.backToSignIn")}</Link>
      </p>
    </section>
  );
}

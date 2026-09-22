import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Trans, useTranslation } from "react-i18next";

import { errorMessage, resendVerification, verifyEmail } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";

/**
 * The code from the sign-up email, typed into the page the customer is already on.
 *
 * A code rather than a link, at the owner's request: a link opened in the phone's mail
 * app lost the page they had open on the laptop. The code is checked against the
 * address, the server confirms the account and opens the session in the same call,
 * and this component takes them to `next` -- the booking, the car, wherever they
 * were going when they were asked to sign up.
 *
 * Shared by the register form (straight after the 202) and by /account/verify, for
 * somebody who closed the tab and comes back with the email in hand.
 *
 * The page is one card with one thing to do, because that is all this step is. When
 * the session cannot be opened -- which happened in production, an IAM grant short --
 * the account still exists, and saying so beats a silent bounce to a form that looks
 * like the one they just filled in.
 */
export default function VerifyCode({ email, next }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [resent, setResent] = useState(false);

  const destination = next || "/account";

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await verifyEmail(email, code);
      if (result.signed_in) {
        // The cookies are already set; the app just has not noticed.
        await refresh();
        navigate(destination, { replace: true });
      } else {
        // Confirmed but not signed in. They have an account and a password, so the
        // sign-in form is the shortest way on -- with a line saying why they are
        // looking at it, rather than landing there with no explanation.
        navigate(`/account/login?next=${encodeURIComponent(destination)}&verified=1`,
                 { replace: true });
      }
    } catch (err) {
      setError(errorMessage(err, t("auth.codeWrong")));
      setBusy(false);
    }
  }

  async function resend() {
    setResent(false);
    setError(null);
    try {
      await resendVerification(email);
    } finally {
      setResent(true);
    }
  }

  return (
    <section className="codecard">
      <h1 className="codecard__title">{t("auth.checkEmail")}</h1>
      <p className="codecard__lead">
        <Trans i18nKey="auth.sentTo" values={{ email }} components={{ 1: <strong /> }} />
      </p>

      <form className="codecard__form" onSubmit={submit}>
        <label className="authform__field">
          <span className="u-visually-hidden">{t("auth.code")}</span>
          <input
            className="codecard__input u-nums"
            value={code}
            onChange={(event) => {
              setError(null);
              setCode(event.target.value.replace(/\D/g, "").slice(0, 6));
            }}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]{6}"
            maxLength={6}
            aria-label={t("auth.code")}
            placeholder="000000"
            required
            autoFocus
          />
        </label>

        {error && <p className="authform__error" role="alert">{error}</p>}

        <button type="submit" className="btn" disabled={busy || code.length !== 6}>
          {busy ? t("auth.verifying") : t("auth.verify")}
        </button>
        <p className="codecard__hint">{t("auth.codeHelp")}</p>
      </form>

      <div className="codecard__quiet">
        <button type="button" className="btn btn--quiet" onClick={resend}>
          {t("auth.resend")}
        </button>
        {resent && <p className="codecard__hint" role="status">{t("auth.resent")}</p>}
      </div>
    </section>
  );
}

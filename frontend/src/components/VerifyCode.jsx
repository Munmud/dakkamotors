import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

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
        navigate(`/account/login?next=${encodeURIComponent(destination)}`, { replace: true });
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
    <section className="authcard">
      <h1 className="section__title">{t("auth.checkEmail")}</h1>
      <p className="state__body">{t("auth.sentTo", { email })}</p>

      <form className="authform" onSubmit={submit}>
        <label className="authform__field">
          <span>{t("auth.code")}</span>
          <input
            className="authform__code u-nums"
            value={code}
            onChange={(event) => {
              setError(null);
              setCode(event.target.value.replace(/\D/g, "").slice(0, 6));
            }}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]{6}"
            maxLength={6}
            required
            autoFocus
          />
        </label>
        <p className="authform__hint">{t("auth.codeHelp")}</p>
        {error && <p className="authform__error" role="alert">{error}</p>}
        <button type="submit" className="btn" disabled={busy || code.length !== 6}>
          {busy ? t("auth.verifying") : t("auth.verify")}
        </button>
      </form>

      <button type="button" className="btn btn--quiet" onClick={resend}>
        {t("auth.resend")}
      </button>
      {resent && <p className="state__body">{t("auth.resent")}</p>}
    </section>
  );
}

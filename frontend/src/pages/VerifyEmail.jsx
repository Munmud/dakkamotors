import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { errorMessage, verifyEmail } from "../lib/auth";

/**
 * The link from the verification email. Clicking it is what makes the account usable.
 *
 * It no longer signs them in. Cognito holds the password from the moment of sign-up and
 * never hands it back, so there is nothing here to authenticate with; verification
 * confirms the account and sends them to the sign-in form with the address filled in.
 * The alternative was keeping a recoverable password for three days, which is worse
 * than one extra screen.
 *
 * The token is read from `window.location` rather than passed to the server in the page
 * request: the CDN cache policy whitelists only `lang`, so every other query string is
 * stripped before the origin ever sees it. In the browser it is simply there.
 */
export default function VerifyEmail() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [state, setState] = useState("working");
  const [error, setError] = useState(null);
  // React 18+ runs effects twice in development; without this the second run spends the
  // now-consumed token and shows a spurious failure.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const token = new URLSearchParams(window.location.search).get("token");
    if (!token) {
      setState("failed");
      setError(t("auth.verifyFailed"));
      return;
    }

    verifyEmail(token)
      .then((result) => {
        setState("done");
        // /account shows the sign-in form to anyone not signed in, and carries `next`
        // through it - so they still land on the booking they were part-way through,
        // one screen later than before.
        const next = encodeURIComponent(result.next || "/account");
        navigate(`/account?next=${next}`, { replace: true });
      })
      .catch((err) => {
        setState("failed");
        setError(errorMessage(err, t("auth.verifyFailed")));
      });
  }, [navigate, t]);

  if (state === "failed") {
    return (
      <section className="state">
        <h1 className="state__title">{t("auth.verifyFailed")}</h1>
        <p className="state__body">{error}</p>
        <Link className="btn" to="/account/register">
          {t("auth.register")}
        </Link>
      </section>
    );
  }

  return (
    <p className="state__body" role="status">
      {state === "done" ? t("auth.verified") : t("auth.verifying")}
    </p>
  );
}

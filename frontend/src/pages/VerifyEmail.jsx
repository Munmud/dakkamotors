import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { errorMessage, verifyEmail } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";

/**
 * The link from the verification email. Clicking it is what creates the account.
 *
 * The token is read from `window.location` rather than passed to the server in the page
 * request: the CDN cache policy whitelists only `lang`, so every other query string is
 * stripped before the origin ever sees it. In the browser it is simply there.
 */
export default function VerifyEmail() {
  const { t } = useTranslation();
  const { refresh } = useAuth();
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
      .then(async (result) => {
        await refresh();
        setState("done");
        // Straight back to the booking they were part-way through.
        navigate(result.next || "/account", { replace: true });
      })
      .catch((err) => {
        setState("failed");
        setError(errorMessage(err, t("auth.verifyFailed")));
      });
  }, [navigate, refresh, t]);

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

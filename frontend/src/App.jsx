import { useEffect } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import CallButton from "./components/CallButton";
import Header from "./components/Header";
import Account from "./pages/Account";
import ResetPassword from "./pages/ResetPassword";
import VerifyEmail from "./pages/VerifyEmail";
import BookTestDrive from "./pages/BookTestDrive";
import CarDetail from "./pages/CarDetail";
import Home from "./pages/Home";
import Privacy from "./pages/Privacy";
import RequestCar from "./pages/RequestCar";
import { AuthProvider } from "./lib/AuthContext";
import { NotificationProvider } from "./lib/NotificationContext";
import { pageView } from "./lib/pixel";

/**
 * The path the pixel has already counted.
 *
 * Module scope rather than a ref, because it has to survive StrictMode mounting this
 * twice in development -- otherwise every local run double-counts, which is exactly
 * the kind of discrepancy that gets blamed on the pixel later.
 */
let counted;

/**
 * Report every navigation after the first.
 *
 * Meta's base snippet fires one PageView, for the document the browser loaded. In a
 * single page app that is the landing page and nothing else -- so a visitor arriving
 * from an advertisement and then browsing the lot would look like a bounce.
 *
 * The first path is recorded without reporting it, because the snippet has counted it
 * already.
 */
function PixelPageViews() {
  const { pathname } = useLocation();
  useEffect(() => {
    if (counted === pathname) return;
    if (counted !== undefined) pageView();
    counted = pathname;
  }, [pathname]);
  return null;
}

function Footer() {
  const { t } = useTranslation();
  const year = new Date().getFullYear();

  return (
    <footer className="footer">
      <div className="footer__inner">
        {/*
          Two columns: where we are, and how to reach us. The address is duplicated into
          the locale files rather than read from `seo.BUSINESS`, which is Python and has
          no bridge to here -- and it genuinely differs by language, which is what the
          locale files are for.

          Each line is its own key, spelled out as a literal. `check-i18n` greps the
          source for `t("...")`, so building these as `t(`footer.addressLine${n}`)` in a
          loop would force the whole `footer` prefix into its dynamic exemption and stop
          every footer key being checked at all.
        */}
        <div className="footer__cols">
          <section className="footer__col">
            <h2 className="footer__head">{t("footer.visit")}</h2>
            <address className="footer__address">
              {t("footer.addressLine1")}
              <br />
              {t("footer.addressLine2")}
              <br />
              <span className="u-nums">{t("footer.addressLine3")}</span>
            </address>
          </section>

          <section className="footer__col">
            <h2 className="footer__head">{t("footer.callPrompt")}</h2>
            <CallButton onInk />
          </section>
        </div>

        <p className="footer__legal u-nums">
          © {year} {t("footer.rights")} ·{" "}
          {/* The site runs a Meta pixel, and Meta's terms require a link to a page
              saying so from where it runs -- which is every page. It sits in the
              legal line rather than the columns because that is where a reader
              looking for it will look. */}
          <Link className="footer__link" to="/privacy">{t("footer.privacy")}</Link>
        </p>
        {/*
          No staff login link here any more. It sat on every page of a public site
          advertising where the admin lives, and bought nothing: staff know the URL, and
          they now get an Admin button in the masthead once signed in.
        */}
      </div>
    </footer>
  );
}

export default function App() {
  return (
    // Inside AuthProvider, because the bell only ever fetches once it knows there is a
    // signed-in customer to fetch for.
    <AuthProvider>
      <NotificationProvider>
        <div className="l-shell">
          <PixelPageViews />
          <Header />
          <main className="l-main">
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/cars/:slug" element={<CarDetail />} />
              <Route path="/cars/:slug/test-drive" element={<BookTestDrive />} />
              <Route path="/request-a-car" element={<RequestCar />} />
              <Route path="/privacy" element={<Privacy />} />
              <Route path="/account" element={<Account />} />
              <Route path="/account/login" element={<Account mode="login" />} />
              <Route path="/account/register" element={<Account mode="register" />} />
              <Route path="/account/verify" element={<VerifyEmail />} />
              <Route path="/account/reset" element={<ResetPassword />} />
            </Routes>
          </main>
          <Footer />
        </div>
      </NotificationProvider>
    </AuthProvider>
  );
}

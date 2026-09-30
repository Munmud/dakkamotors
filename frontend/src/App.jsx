import { useEffect } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import CallButton from "./components/CallButton";
import Header from "./components/Header";
import CarDetail from "./pages/CarDetail";
import Home from "./pages/Home";
import Privacy from "./pages/Privacy";
import RequestCar from "./pages/RequestCar";
import { pageView } from "./lib/pixel";

/* The address as a maps query, in Japanese and in Japanese order, because that is
   what Google Maps resolves in Japan. Hard-coded rather than built from the three
   footer lines: those are translated, and an English transliteration finds nothing. */
const MAP_QUERY = encodeURIComponent("東京都羽村市神明台1丁目16-2 神明台店舗");

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
            {/* A link, not three lines of text. Somebody reading this is ten
                minutes away in Fussa or Ome and the most useful thing the address
                can do is open their maps app -- otherwise they have to select and
                copy Japanese text on a phone. */}
            <address className="footer__address">
              <a className="footer__map"
                 href={`https://www.google.com/maps/search/?api=1&query=${MAP_QUERY}`}
                 target="_blank" rel="noreferrer">
                {t("footer.addressLine1")}
                <br />
                {t("footer.addressLine2")}
                <br />
                <span className="u-nums">{t("footer.addressLine3")}</span>
              </a>
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
              saying so from where it runs -- which is every page. */}
          <Link className="footer__link" to="/privacy">{t("footer.privacy")}</Link>
          {" · "}
          {/*
            The way into the admin. A plain anchor, not a router Link: /api/staff/ is
            rendered by Django, so React must hand the browser over rather than route it.

            It lived in the masthead until customer accounts went, appearing only once
            `/api/auth/me/` came back with `is_staff`. There is no `/api/auth/me/` now
            and no session for the app to read, so the only options were a link
            somebody can always see or staff typing the URL from memory. Down here,
            in the legal line, it is out of a buyer's way and findable by the two
            people who need it.
          */}
          <a className="footer__link" href="/api/staff/">{t("footer.staff")}</a>
        </p>
      </div>
    </footer>
  );
}

export default function App() {
  const { t } = useTranslation();

  return (
    <div className="l-shell">
      <PixelPageViews />
      {/*
        Hidden until it is tabbed to, then the first thing on the page.

        A car page puts fourteen stops in front of the name field -- the monogram,
        the number, two language buttons, the back link and up to eight gallery
        thumbnails -- and does it again on every car. Somebody using a keyboard, or a
        switch, or a screen reader, should not have to walk the masthead to leave a
        phone number.
      */}
      <a className="skiplink" href="#main">{t("nav.skip")}</a>
      <Header />
      <main className="l-main" id="main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/cars/:slug" element={<CarDetail />} />
          <Route path="/request-a-car" element={<RequestCar />} />
          <Route path="/privacy" element={<Privacy />} />
        </Routes>
      </main>
      <Footer />
    </div>
  );
}

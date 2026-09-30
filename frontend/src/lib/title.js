import { useEffect } from "react";

/**
 * Keep `document.title` in step with the page the app is showing.
 *
 * `pages.py` sets a correct, per-page title on the server, so a direct load or a
 * crawler always sees the right one. The app never touched it again — so the moment
 * somebody navigated inside the site, the tab, the history entry and the screen
 * reader's page announcement all stayed on whatever page they had arrived at. Open
 * three cars from the lot and the browser's history reads as the same page three
 * times.
 *
 * The suffix matches `seo.SITE_NAME` on the server, so a client-rendered title and a
 * server-rendered one are the same string rather than merely similar.
 */
const SUFFIX = "Dakka Motors";
const SUFFIX_JA = "ダッカモータース";

export function usePageTitle(title, language) {
  useEffect(() => {
    if (!title) return;
    const suffix = language === "ja" ? SUFFIX_JA : SUFFIX;
    // The full-width bar is the Japanese convention and the server uses it too.
    document.title = language === "ja"
      ? `${title}｜${suffix}`
      : `${title} | ${suffix}`;
  }, [title, language]);
}

import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./en.json";
import ja from "./ja.json";

export const LANGUAGES = ["en", "ja"];
const STORAGE_KEY = "dakka.lang";

/**
 * Japanese, unless this visitor has chosen otherwise before.
 *
 * It used to read `navigator.language`. The reason it does not any more is not that
 * the guess was bad — it is that the **server cannot make the same guess**.
 * `cars/pages.py` renders the shell before any JavaScript runs and cannot see
 * `navigator`; it was reading `Accept-Language`, and the two disagreed often enough
 * that a visitor could be served one language and watch it flip to the other on
 * mount. A visible flash, on the one page an advertisement paid for.
 *
 * So there is one fixed default, chosen in two places that have to say the same
 * thing: here and `pages._language_from`. Japanese, because the shop is in Hamura and
 * that is what most people who walk in speak.
 *
 * A saved choice still wins, and the switcher still works.
 *
 * **`?lang=` wins over both, and must.** It is not a convenience: `pages.py` renders
 * the server shell in the language the query string asks for and publishes
 * `<link rel="alternate" hreflang="en" href="...?lang=en">`, so that URL is what
 * Google is told to serve an English searcher and what gets pasted into a message.
 * Without this the server renders English, React mounts, ignores the query string
 * and repaints the page in Japanese — the English page existed for about 200ms.
 *
 * It went unnoticed because the server side has a test and reads correctly, and
 * because a screenshot pass that visited `?lang=en` photographed a Japanese page
 * fourteen times and nobody compared the files.
 */
function initialLanguage() {
  try {
    const asked = new URLSearchParams(window.location.search).get("lang");
    if (asked && LANGUAGES.includes(asked)) return asked;
  } catch {
    // A malformed query string is not a reason to fail to start.
  }
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && LANGUAGES.includes(saved)) return saved;
  } catch {
    // Private browsing or blocked storage — fall through to the default.
  }
  return "ja";
}

i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, ja: { translation: ja } },
  lng: initialLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

/** Keep <html lang> in step so screen readers and font fallback pick the right language. */
function syncDocumentLanguage(lng) {
  document.documentElement.lang = lng;
}
syncDocumentLanguage(i18n.language);

i18n.on("languageChanged", (lng) => {
  syncDocumentLanguage(lng);
  try {
    localStorage.setItem(STORAGE_KEY, lng);
  } catch {
    // Not being able to remember the choice is not worth breaking the page over.
  }
});

export default i18n;

import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./en.json";
import ja from "./ja.json";

export const LANGUAGES = ["en", "ja"];
const STORAGE_KEY = "dakka.lang";

function initialLanguage() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && LANGUAGES.includes(saved)) return saved;
  } catch {
    // Private browsing or blocked storage — fall through to the browser preference.
  }
  return navigator.language?.toLowerCase().startsWith("ja") ? "ja" : "en";
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

import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { askQuestion, errorMessage, fetchMyQuestions } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";

/**
 * Questions about one car.
 *
 * Published pairs are shown to everyone, anonymously — they arrive with the car payload,
 * already server-rendered into the page, so a visitor with no JavaScript and a crawler
 * both see them. Filtering by language happens here rather than on the server because
 * the API response is cached at the CDN without a language key, and doing it in the
 * browser means the language switch re-filters with no refetch.
 *
 * A signed-in customer also sees their own thread, including answers that have not been
 * published. That matters: answering and publishing are separate decisions, so someone
 * can be told "we have answered you" about an answer that is nowhere on the page.
 */
export default function CarQuestions({ car }) {
  const { t, i18n } = useTranslation();
  const { customer, state } = useAuth();
  const { pathname } = useLocation();

  const [mine, setMine] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);
  const slug = car.slug;

  const published = useMemo(() => {
    const all = car.questions ?? [];
    const matching = all.filter((q) => q.language === i18n.language);
    // An answer in the wrong language beats an empty section.
    return matching.length ? matching : all;
  }, [car.questions, i18n.language]);

  useEffect(() => {
    let cancelled = false;
    if (!customer) {
      setMine([]);
      return undefined;
    }
    fetchMyQuestions(slug)
      .then((rows) => !cancelled && setMine(rows))
      .catch(() => !cancelled && setMine([]));
    return () => {
      cancelled = true;
    };
  }, [customer, slug]);

  // Their own question, still waiting. Published ones are already in the list above.
  const pending = mine.filter((q) => !q.is_published);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await askQuestion({
        car: slug,
        question: text,
        language: i18n.language,
      });
      setMine((current) => [created, ...current]);
      setText("");
      setSent(true);
    } catch (err) {
      setError(errorMessage(err, t("error.body")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section">
      <h2 className="section__title">{t("qa.heading")}</h2>

      {published.length > 0 ? (
        <dl className="qa">
          {published.map((q) => (
            <div className="qa__pair" key={q.id}>
              <dt className="qa__question">{q.question}</dt>
              <dd className="qa__answer">{q.answer}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="state__body">{t("qa.none")}</p>
      )}

      {pending.length > 0 && (
        <ul className="qa__mine">
          {pending.map((q) => (
            <li className="qa__mineitem" key={q.id}>
              <p className="qa__question">{q.question}</p>
              {q.answer ? (
                <p className="qa__answer">{q.answer}</p>
              ) : (
                <p className="qa__waiting">{t("qa.awaitingAnswer")}</p>
              )}
            </li>
          ))}
        </ul>
      )}

      {state === "unknown" ? null : customer ? (
        <form className="qa__form" onSubmit={submit}>
          <label className="authform__field">
            <span>{t("qa.ask")}</span>
            <textarea
              className="qa__input"
              rows={3}
              maxLength={1000}
              value={text}
              onChange={(event) => {
                setSent(false);
                setText(event.target.value);
              }}
              placeholder={t("qa.placeholder")}
              required
            />
          </label>
          {error && <p className="authform__error" role="alert">{error}</p>}
          {sent && <p className="authform__saved" role="status">{t("qa.sent")}</p>}
          <button type="submit" className="btn" disabled={busy || !text.trim()}>
            {busy ? t("qa.sending") : t("qa.submit")}
          </button>
        </form>
      ) : (
        <p className="state__body">
          {/* Carries them back to this car once they are signed in. */}
          <Link to={`/account/login?next=${encodeURIComponent(pathname)}`}>
            {t("qa.signInToAsk")}
          </Link>
        </p>
      )}
    </section>
  );
}

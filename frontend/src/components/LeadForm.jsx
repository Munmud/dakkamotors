import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { errorMessage, sendLead } from "../api/client";
import { sentLead } from "../lib/pixel";
import { phoneDisplay, phoneHref } from "../lib/format";

/**
 * The one thing this site is for: a name and a phone number.
 *
 * Every decision here is about the same person — somebody who tapped a video
 * advertisement thirty seconds ago, has never heard of this shop, and will close the
 * tab at the first sign of work.
 *
 * **On a car page it is two fields: a name and a number.** Nothing else at all — no
 * email, no message box. A form of three fields or fewer completes at roughly 25%
 * against 10% for anything longer, and this is two. There is nothing for a message to
 * say that the page it was submitted from has not already said: the lead carries the
 * car, and the shop is going to ring them anyway.
 *
 * **On the find-a-car page it is three, and the message is required** (`wish`). There
 * the message *is* the request — "a Tanto, under ¥500,000, by March" is the only
 * content the lead has, and without it staff have a phone number and no reason to
 * ring it. Email stays optional there because a wish list is the one thing somebody
 * might genuinely want in writing.
 *
 * No account, no password, no confirmation code: the previous version of this site
 * asked a stranger to create an account before it would show them a calendar, and it
 * converted nobody.
 *
 * **Calling is offered beside it, not instead of it.** A tap-to-call gets several
 * times the engagement of a printed number on a phone, and the two do different jobs:
 * calling suits somebody ready now, the form suits somebody browsing at midnight.
 * Neither is buried.
 *
 * On success the form is replaced by the confirmation, not merely cleared. A cleared
 * form reads as "that did not work, try again", which is how a shop gets the same
 * lead three times.
 */
export default function LeadForm({ car = null, wish = false, id, sent, onSent,
                                  reserved = false, bare = false }) {
  const { t, i18n } = useTranslation();
  const [values, setValues] = useState({ name: "", phone: "", email: "", details: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [ownDone, setOwnDone] = useState(false);

  // A car page renders this twice -- once beside the price, once after the specs --
  // and `sent` is how the second one knows the first has already been submitted.
  // Without it somebody fills the top form, scrolls past the specs, finds an empty
  // form asking the same question and concludes it did not go through. The docstring
  // above says a cleared form is how a shop receives the same lead three times; two
  // independent copies of this component are that same bug wearing a hat.
  const done = sent ?? ownDone;

  const fields = useRef({});

  const set = (field) => (event) => {
    setError(null);
    setValues((current) => ({ ...current, [field]: event.target.value }));
  };

  /**
   * The first field that still needs something, or null.
   *
   * Used to *answer* a submit, not to prevent one. The button used to be `disabled`
   * until everything was filled, and on the dark theme a disabled button is the same
   * grey as the panel behind it -- so the only bright, obviously-pressable thing on
   * the page appeared *after* the visitor had already done all the work, and the
   * form they were looking at before that had two empty boxes and no visible button.
   * A disabled control also never says why it is disabled; somebody who fills one
   * field, looks up and sees a dead button concludes the page is broken.
   */
  function missing() {
    if (!values.name.trim()) return "name";
    if (!values.phone.trim()) return "phone";
    if (wish && !values.details.trim()) return "details";
    return null;
  }

  async function submit(event) {
    event.preventDefault();
    if (busy) return;

    const gap = missing();
    if (gap) {
      setError(t(`lead.need.${gap}`));
      fields.current[gap]?.focus();
      return;
    }

    setBusy(true);
    setError(null);
    try {
      await sendLead({ ...values, car, language: i18n.language });
      sentLead(car);
      setOwnDone(true);
      onSent?.();
    } catch (err) {
      setError(errorMessage(err, t("lead.failed")));
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      // `role="status"` so a screen reader hears it. The form that had focus is
      // unmounted by this swap, so without an announcement somebody who cannot see
      // the page presses the button and gets silence. The error path already had
      // `role="alert"`; the success path should not be the quieter of the two.
      <div className="lead lead--done" id={id} role="status">
        <p className="lead__doneTitle">{t("lead.doneTitle")}</p>
        <p className="lead__doneBody">{t("lead.doneBody")}</p>
        {/* The number stays. This is the moment of highest intent on the whole site
            and the copy says "call us if it is urgent" -- so the thing to call has
            to be under it, not two screens up in the masthead. */}
        <p className="lead__or">
          {t("lead.orCall")}{" "}
          <a className="lead__tel u-nums" href={`tel:${phoneHref}`}>{phoneDisplay}</a>
        </p>
      </div>
    );
  }

  return (
    <form className="lead" id={id} onSubmit={submit} noValidate>
      {/* `bare` when the section around it already carries a heading. Two polite
          questions eight pixels apart -- "この車をご覧になりますか？" above
          "この車が気になりますか？" -- is the visual signature of a template
          repeating itself, and it was doing it in the most valuable slot on the
          page. */}
      {!bare && (
        <p className="lead__title">
          {reserved ? t("lead.titleReserved")
            : car ? t("lead.titleCar") : t("lead.titleGeneral")}
        </p>
      )}
      <p className="lead__why">{reserved ? t("lead.whyReserved") : t("lead.why")}</p>

      {error && <p className="lead__error" role="alert">{error}</p>}

      <label className="lead__field">
        <span>{t("lead.name")}</span>
        <input ref={(el) => { fields.current.name = el; }}
               value={values.name} onChange={set("name")} autoComplete="name"
               required enterKeyHint="next" />
      </label>

      <label className="lead__field">
        <span>{t("lead.phone")}</span>
        {/* `type="tel"` and `inputMode` so a phone shows the keypad rather than a
            qwerty keyboard. No pattern: numbers here are typed as 080-1234-5678,
            08012345678 and +81 80 1234 5678, and refusing any of those loses a lead
            to gain nothing a person cannot read. */}
        <input ref={(el) => { fields.current.phone = el; }}
               type="tel" inputMode="tel" value={values.phone} onChange={set("phone")}
               autoComplete="tel" required enterKeyHint={wish ? "next" : "send"} />
      </label>

      {/* Email and the message exist only on the find-a-car form. On a car page they
          are two more things to read past on the way to a button, and neither earns
          its place: the lead already names the car, and the shop telephones. */}
      {wish && (
        <>
          <label className="lead__field">
            <span>{t("lead.email")} <em>{t("lead.optional")}</em></span>
            <input type="email" value={values.email} onChange={set("email")}
                   autoComplete="email" enterKeyHint="next" />
          </label>

          {/* Not "anything you want to ask". On this page the message *is* the
              request -- there is no car to infer it from -- so the label names the
              thing rather than inviting a question. */}
          <label className="lead__field">
            <span>{t("lead.wish")}</span>
            <textarea ref={(el) => { fields.current.details = el; }}
                      rows={5} value={values.details} onChange={set("details")}
                      maxLength={2000} placeholder={t("lead.wishHint")} />
          </label>
        </>
      )}

      {/* Green from the moment the page loads. Only `busy` disables it, and that is
          a real reason -- the request is in flight. */}
      <button type="submit" className="btn btn--go" disabled={busy}>
        {busy ? t("lead.sending") : t("lead.send")}
      </button>

      <p className="lead__or">
        {t("lead.orCall")}{" "}
        <a className="lead__tel u-nums" href={`tel:${phoneHref}`}>{phoneDisplay}</a>
      </p>

      {/* At the point of collection, not only in the footer. APPI expects the
          purpose of use to be stated where personal data is taken, Meta's ad review
          looks for it, and more usefully it is a visible reassurance at the exact
          second somebody hesitates over typing their number. */}
      <p className="lead__privacy">
        <Link to="/privacy">{t("lead.privacyNote")}</Link>
      </p>
    </form>
  );
}

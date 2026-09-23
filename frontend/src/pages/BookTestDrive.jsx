import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import SlotPicker from "../components/SlotPicker";
import { fetchCar } from "../api/client";
import { bookSlot, errorMessage, fetchMyBookings, fetchSlots } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";
import { formatSlotFull } from "../lib/datetime";
import { carTitle, phoneDisplay } from "../lib/format";
import { bookedTestDrive, startedBooking } from "../lib/pixel";

/**
 * Pick a time to test drive one specific car.
 *
 * Availability is shown to anonymous visitors deliberately: making someone register
 * before they can tell whether any time suits them is a good way to lose them. Sign-in
 * is asked for only at the point of confirming.
 */
export default function BookTestDrive() {
  const { slug } = useParams();
  const { t, i18n } = useTranslation();
  const { customer, state } = useAuth();

  const [car, setCar] = useState(null);
  const [slots, setSlots] = useState([]);
  const [selected, setSelected] = useState(null);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState(null);
  const [done, setDone] = useState(false);
  // Their live booking for this car, if they have one. The server refuses a second
  // either way; this is so nobody picks a time before being told.
  const [already, setAlready] = useState(null);
  // Only a guest fills these in; a signed-in customer's are already on their account.
  const [guest, setGuest] = useState({ name: "", email: "", phone: "" });

  useEffect(() => {
    if (state === "unknown") return undefined;
    let cancelled = false;
    // A guest has no bookings to fetch, and asking would be a guaranteed 403.
    const mine = customer ? fetchMyBookings().catch(() => []) : Promise.resolve([]);
    Promise.all([fetchCar(slug), fetchSlots(), mine])
      .then(([carData, slotData, bookings]) => {
        if (cancelled) return;
        setCar(carData);
        setSlots(slotData);
        // The endpoint returns active bookings only, so anything here for this car
        // is live: pending or confirmed.
        setAlready(bookings.find((booking) => booking.car_slug === slug) ?? null);
        setStatus("ready");
      })
      .catch(() => !cancelled && setStatus("error"));
    return () => {
      cancelled = true;
    };
  }, [slug, customer, state]);

  // Opening this page is the step that used to meet the account wall, so it is the
  // number worth watching against the one below it: how many of the people an
  // advertisement sent to a car went as far as looking for a time.
  useEffect(() => {
    startedBooking(slug);
  }, [slug]);

  const set = (field) => (event) => {
    setError(null);
    setGuest((current) => ({ ...current, [field]: event.target.value }));
  };

  const guestReady = Boolean(
    guest.name.trim() && guest.email.trim() && guest.phone.trim());

  async function confirm() {
    if (!selected) return;
    setError(null);
    setStatus("saving");
    try {
      await bookSlot({ slot: selected, car: slug, guest: customer ? null : guest });
      // Only after the server accepted it. A booking the calendar refused is not a
      // conversion, and counting it would overstate exactly the number the decision
      // to keep paying for an advertisement rests on.
      bookedTestDrive(slug);
      setDone(true);
      setStatus("ready");
    } catch (err) {
      // Someone may have taken the last place between loading and clicking, so the
      // list is refreshed rather than left showing a slot that is gone.
      setError(errorMessage(err, t("error.body")));
      setSlots(await fetchSlots());
      setSelected(null);
      setStatus("ready");
    }
  }

  if (status === "loading") return <p className="state__body">{t("loading")}…</p>;
  if (status === "error") {
    return (
      <div className="state">
        <h2 className="state__title">{t("error.title")}</h2>
        <p className="state__body">{t("error.body")}</p>
      </div>
    );
  }

  // One live test drive per car. Said here rather than at the confirm button, for the
  // same reason the details are asked for after a time is picked: being turned away
  // having already chosen wastes the choice.
  if (already) {
    return (
      <section>
        <Link className="backlink" to={`/cars/${slug}`}>
          {car ? carTitle(car, i18n.language) : t("nav.back")}
        </Link>
        <h1 className="section__title">{t("booking.heading")}</h1>
        {car && <p className="state__body">{t("booking.forCar", { car: carTitle(car, i18n.language) })}</p>}
        <div className="state">
          <h2 className="state__title">{t("booking.alreadyBooked")}</h2>
          <p className="state__body">
            {t("booking.alreadyBookedOn",
               { when: formatSlotFull(already.starts_at, i18n.language) })}
          </p>
          <Link className="callbtn bookbtn" to="/account">
            {t("booking.seeMyBookings")}
          </Link>
        </div>
      </section>
    );
  }

  if (done) {
    return (
      <section className="state">
        <h1 className="state__title">{t("booking.requested")}</h1>
        <p className="state__body">
          {t("booking.requestedNote", { phone: phoneDisplay })}
        </p>
        <Link className="btn" to="/account">
          {t("booking.myBookings")}
        </Link>
      </section>
    );
  }

  return (
    <section>
      <Link className="backlink" to={`/cars/${slug}`}>
        {car ? carTitle(car, i18n.language) : t("nav.back")}
      </Link>

      <h1 className="section__title">{t("booking.heading")}</h1>
      {car && <p className="state__body">{t("booking.forCar", { car: carTitle(car, i18n.language) })}</p>}

      {error && <p className="authform__error" role="alert">{error}</p>}

      <SlotPicker
        slots={slots}
        selected={selected}
        onSelect={setSelected}
        disabled={status === "saving"}
      />

      {/* A guest gives the three details the shop needs to confirm the appointment,
          and only once they have chosen a time -- asking for them up front, before
          anything has been decided, is the wall this page used to put in front of
          every click from an advertisement. */}
      {selected && !customer && state !== "unknown" && (
        <div className="bookdetails">
          <h2 className="bookdetails__title">{t("booking.yourDetails")}</h2>
          <p className="bookdetails__hint">{t("booking.yourDetailsWhy")}</p>
          <div className="authform">
            <label className="authform__field">
              <span>{t("auth.name")}</span>
              <input value={guest.name} onChange={set("name")} required
                     autoComplete="name" />
            </label>
            <label className="authform__field">
              <span>{t("auth.email")}</span>
              <input type="email" value={guest.email} onChange={set("email")} required
                     autoComplete="email" />
            </label>
            <label className="authform__field">
              <span>{t("auth.phone")}</span>
              <input type="tel" value={guest.phone} onChange={set("phone")} required
                     autoComplete="tel" inputMode="tel" />
            </label>
          </div>
          <p className="bookdetails__hint">
            {t("auth.haveAccount")}{" "}
            <Link to={`/account/login?next=${encodeURIComponent(`/cars/${slug}/test-drive`)}`}>
              {t("auth.signIn")}
            </Link>
          </p>
        </div>
      )}

      {slots.length > 0 && (
        <button
          type="button"
          className="callbtn bookbtn"
          disabled={!selected || status === "saving" || (!customer && !guestReady)}
          onClick={confirm}
        >
          {status === "saving" ? t("booking.booking") : t("booking.confirm")}
        </button>
      )}
    </section>
  );
}

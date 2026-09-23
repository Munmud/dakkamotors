import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import SlotPicker from "../components/SlotPicker";
import { fetchCar } from "../api/client";
import { bookSlot, errorMessage, fetchMyBookings, fetchSlots } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";
import { formatSlotFull } from "../lib/datetime";
import { carTitle, phoneDisplay } from "../lib/format";

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

  async function confirm() {
    if (!selected || !customer) return;
    setError(null);
    setStatus("saving");
    try {
      await bookSlot({ slot: selected, car: slug });
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

  // A guest is asked before the calendar, not after picking a time. Choosing a slot
  // and only then being sent away to sign up lost the choice and, until the code
  // flow, the page; asking first means the calendar they come back to is the one
  // they can actually book on. The register page, not sign-in: a first-time visitor
  // has no account yet, and the form's switch link is there for the ones who do.
  if (state === "anonymous") {
    const here = encodeURIComponent(`/cars/${slug}/test-drive`);
    return (
      <section>
        <Link className="backlink" to={`/cars/${slug}`}>
          {car ? carTitle(car, i18n.language) : t("nav.back")}
        </Link>
        <h1 className="section__title">{t("booking.heading")}</h1>
        {car && <p className="state__body">{t("booking.forCar", { car: carTitle(car, i18n.language) })}</p>}
        <div className="state">
          <h2 className="state__title">{t("booking.signUpFirst")}</h2>
          <p className="state__body">{t("booking.signUpFirstBody")}</p>
          <Link className="callbtn bookbtn" to={`/account/register?next=${here}`}>
            {t("auth.register")}
          </Link>
          <p className="state__body">
            <Link to={`/account/login?next=${here}`}>{t("auth.haveAccount")}</Link>
          </p>
        </div>
      </section>
    );
  }

  // One live test drive per car. Said here rather than at the confirm button, for the
  // same reason the sign-up prompt moved: picking a time and only then being turned
  // away wastes the choice they just made.
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

      {slots.length > 0 && (
        <button
          type="button"
          className="callbtn bookbtn"
          disabled={!selected || status === "saving"}
          onClick={confirm}
        >
          {status === "saving" ? t("booking.booking") : t("booking.confirm")}
        </button>
      )}
    </section>
  );
}

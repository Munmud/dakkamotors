import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import SlotPicker from "../components/SlotPicker";
import { fetchCar } from "../api/client";
import { bookSlot, errorMessage, fetchSlots } from "../lib/auth";
import { useAuth } from "../lib/AuthContext";
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
  const { t } = useTranslation();
  const { customer, state } = useAuth();
  const navigate = useNavigate();

  const [car, setCar] = useState(null);
  const [slots, setSlots] = useState([]);
  const [selected, setSelected] = useState(null);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchCar(slug), fetchSlots()])
      .then(([carData, slotData]) => {
        if (cancelled) return;
        setCar(carData);
        setSlots(slotData);
        setStatus("ready");
      })
      .catch(() => !cancelled && setStatus("error"));
    return () => {
      cancelled = true;
    };
  }, [slug]);

  async function confirm() {
    if (!selected) return;
    if (!customer) {
      // Keep their place: come back here once they are signed in.
      navigate(`/account/login?next=${encodeURIComponent(`/cars/${slug}/test-drive`)}`);
      return;
    }
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
        {car ? carTitle(car) : t("nav.back")}
      </Link>

      <h1 className="section__title">{t("booking.heading")}</h1>
      {car && <p className="state__body">{t("booking.forCar", { car: carTitle(car) })}</p>}

      <h2 className="section__title">{t("booking.chooseTime")}</h2>
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
          {status === "saving"
            ? t("booking.booking")
            : customer || state === "unknown"
              ? t("booking.confirm")
              : t("booking.signInToBook")}
        </button>
      )}
    </section>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import SlotPicker from "../components/SlotPicker";
import { formatSlotFull } from "../lib/datetime";
import { useAuth } from "../lib/AuthContext";
import {
  cancelBooking,
  errorMessage,
  fetchMyBookings,
  fetchSlots,
  login,
  logout,
  register,
  rescheduleBooking,
  resendVerification,
} from "../lib/auth";
import { phoneDisplay } from "../lib/format";

/**
 * Sign in, create an account, and manage your own test drives.
 *
 * One page with three modes rather than three routes: a customer bounces between
 * signing in and registering constantly, and the booking list is where both of them
 * end up.
 */
export default function Account({ mode = "bookings" }) {
  const { t } = useTranslation();
  const { customer, state, refresh, setCustomer } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const next = params.get("next");

  if (state === "unknown") return <p className="state__body">{t("loading")}…</p>;

  if (!customer) {
    return (
      <AuthForm
        next={next}
        mode={mode === "register" ? "register" : "login"}
        onDone={async () => {
          await refresh();
          navigate(next || "/account", { replace: true });
        }}
      />
    );
  }

  return (
    <MyBookings
      customer={customer}
      onSignOut={async () => {
        await logout();
        setCustomer(null);
        navigate("/", { replace: true });
      }}
    />
  );
}

function AuthForm({ mode, onDone, next }) {
  const { t, i18n } = useTranslation();
  const registering = mode === "register";
  const [values, setValues] = useState({ name: "", email: "", phone: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [sentTo, setSentTo] = useState(null);
  const [resent, setResent] = useState(false);

  const set = (field) => (event) =>
    setValues((current) => ({ ...current, [field]: event.target.value }));

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (registering) {
        // No account exists yet; the link in the email is what creates it.
        await register({ ...values, next, language: i18n.language });
        setSentTo(values.email);
      } else {
        await login({ email: values.email, password: values.password });
        await onDone();
      }
    } catch (err) {
      setError(errorMessage(err, t("auth.failed")));
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    setResent(false);
    try {
      await resendVerification(sentTo);
    } finally {
      setResent(true);
    }
  }

  if (sentTo) {
    return (
      <section className="authcard">
        <h1 className="section__title">{t("auth.checkEmail")}</h1>
        <p className="state__body">{t("auth.sentTo", { email: sentTo })}</p>
        <button type="button" className="btn btn--quiet" onClick={resend}>
          {t("auth.resend")}
        </button>
        {resent && <p className="state__body">{t("auth.resent")}</p>}
      </section>
    );
  }

  return (
    <section className="authcard">
      <h1 className="section__title">
        {registering ? t("auth.register") : t("auth.signIn")}
      </h1>

      <form className="authform" onSubmit={submit}>
        {registering && (
          <>
            <label className="authform__field">
              <span>{t("auth.name")}</span>
              <input value={values.name} onChange={set("name")} required autoComplete="name" />
            </label>
            <label className="authform__field">
              <span>{t("auth.phone")}</span>
              <input
                value={values.phone}
                onChange={set("phone")}
                required
                inputMode="tel"
                autoComplete="tel"
              />
            </label>
          </>
        )}

        <label className="authform__field">
          <span>{t("auth.email")}</span>
          <input
            type="email"
            value={values.email}
            onChange={set("email")}
            required
            autoComplete="email"
          />
        </label>

        <label className="authform__field">
          <span>{t("auth.password")}</span>
          <input
            type="password"
            value={values.password}
            onChange={set("password")}
            required
            autoComplete={registering ? "new-password" : "current-password"}
          />
        </label>

        {error && <p className="authform__error" role="alert">{error}</p>}

        <button type="submit" className="btn" disabled={busy}>
          {busy
            ? registering
              ? t("auth.creating")
              : t("auth.signingIn")
            : registering
              ? t("auth.register")
              : t("auth.signIn")}
        </button>
      </form>

      <p className="authform__switch">
        <Link to={registering ? "/account/login" : "/account/register"}>
          {registering ? t("auth.haveAccount") : t("auth.needAccount")}
        </Link>
        {!registering && (
          <>
            {" · "}
            <Link to="/account/reset">{t("auth.forgot")}</Link>
          </>
        )}
      </p>
    </section>
  );
}

function MyBookings({ customer, onSignOut }) {
  const { t, i18n } = useTranslation();
  const [bookings, setBookings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [rescheduling, setRescheduling] = useState(null);
  const [slots, setSlots] = useState([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setBookings(await fetchMyBookings());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function openReschedule(id) {
    setError(null);
    setSlots(await fetchSlots());
    setRescheduling(id);
  }

  async function move(bookingId, slotId) {
    setError(null);
    try {
      await rescheduleBooking(bookingId, slotId);
      setRescheduling(null);
      await load();
    } catch (err) {
      setError(errorMessage(err, t("error.body")));
    }
  }

  async function drop(id) {
    if (!window.confirm(t("booking.confirmCancel"))) return;
    setError(null);
    try {
      await cancelBooking(id);
      await load();
    } catch (err) {
      setError(errorMessage(err, t("error.body")));
    }
  }

  return (
    <section>
      <div className="account__head">
        <h1 className="section__title">{t("booking.myBookings")}</h1>
        <button type="button" className="btn btn--quiet" onClick={onSignOut}>
          {t("auth.signOut")}
        </button>
      </div>
      <p className="state__body">{customer.name} · {customer.email}</p>

      {error && <p className="authform__error" role="alert">{error}</p>}

      {loading ? (
        <p className="state__body">{t("loading")}…</p>
      ) : bookings.length === 0 ? (
        <p className="state__body">{t("booking.none")}</p>
      ) : (
        <ul className="bookings">
          {bookings.map((booking) => (
            <li className="bookings__item" key={booking.id}>
              <div>
                <p className="bookings__when u-nums">
                  {formatSlotFull(booking.starts_at, i18n.language)}
                </p>
                {/* Says plainly whether we have agreed to it, so nobody drives over
                    for a slot that is still only a request. */}
                <p
                  className={
                    booking.status === "confirmed"
                      ? "bookings__status bookings__status--ok"
                      : "bookings__status"
                  }
                >
                  {booking.status === "confirmed"
                    ? t("booking.confirmed")
                    : t("booking.awaiting")}
                </p>
                {booking.car_label && (
                  <p className="bookings__car">{booking.car_label}</p>
                )}
              </div>
              <div className="bookings__actions">
                <button type="button" className="btn btn--quiet" onClick={() => openReschedule(booking.id)}>
                  {t("booking.reschedule")}
                </button>
                <button type="button" className="btn btn--quiet" onClick={() => drop(booking.id)}>
                  {t("booking.cancel")}
                </button>
              </div>

              {rescheduling === booking.id && (
                <div className="bookings__picker">
                  <SlotPicker
                    // Remounts per booking, so the picker always opens on its date step
                    // rather than on the day the previous reschedule was left showing.
                    key={rescheduling}
                    slots={slots}
                    selected={null}
                    // Picking a time here reschedules immediately. The picker also
                    // reports null when someone goes back to change the date, which is
                    // not a choice and must not move the booking.
                    onSelect={(slotId) => slotId && move(booking.id, slotId)}
                  />
                  <button
                    type="button"
                    className="btn btn--quiet"
                    onClick={() => setRescheduling(null)}
                  >
                    {t("booking.keepTime")}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      <p className="state__body">{t("booking.callNote", { phone: phoneDisplay })}</p>
    </section>
  );
}

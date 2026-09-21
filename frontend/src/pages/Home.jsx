import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { fetchCars } from "../api/client";
import CarCard from "../components/CarCard";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { takeInitialData } from "../lib/initialData";

// Present on a fresh page load, absent after a client-side navigation.
const seeded = takeInitialData("home");

/**
 * The front page is three things in order: the stock, one question, and the cars
 * that have already gone.
 *
 * The stock is available then reserved, marked. The question -- "not seeing the car
 * you want?" -- is the page's one action and sits between the two lists, where a
 * buyer who has just reached the end of what is for sale arrives at it. The sold
 * shelf is below because it answers a different question ("what does this shop
 * sell?") and should never be mistaken for stock: faded, marked, no price.
 */
export default function Home() {
  const { t } = useTranslation();
  const [cars, setCars] = useState(seeded?.results ?? []);
  const [nextPage, setNextPage] = useState(seeded?.next ? 2 : null);
  const [status, setStatus] = useState(seeded ? "ready" : "loading");
  const [attempt, setAttempt] = useState(0);
  const [sold, setSold] = useState([]);

  useEffect(() => {
    // The server already sent this page's cars; re-fetching would only make the list
    // flicker.
    if (seeded && attempt === 0) return undefined;

    const controller = new AbortController();
    setStatus("loading");

    fetchCars({ page: 1, signal: controller.signal })
      .then((data) => {
        setCars(data.results);
        setNextPage(data.next ? 2 : null);
        setStatus("ready");
      })
      .catch((error) => {
        if (error.name === "CanceledError") return;
        setStatus("error");
      });

    return () => controller.abort();
  }, [attempt]);

  // The sold shelf is not in the server's initial data -- the page embeds one Query
  // and this is a second. It is fetched after first paint, cached at the edge, and a
  // failure simply leaves the shelf out; nothing above it depends on it.
  useEffect(() => {
    const controller = new AbortController();
    fetchCars({ status: "sold", signal: controller.signal })
      .then((data) => setSold(data.results ?? []))
      .catch(() => setSold([]));
    return () => controller.abort();
  }, [attempt]);

  const loadMore = useCallback(() => {
    if (!nextPage) return;
    fetchCars({ page: nextPage })
      .then((data) => {
        setCars((existing) => [...existing, ...data.results]);
        setNextPage(data.next ? nextPage + 1 : null);
      })
      .catch(() => setNextPage(null));
  }, [nextPage]);

  if (status === "loading") return <LoadingState />;
  if (status === "error") {
    return <ErrorState onRetry={() => setAttempt((n) => n + 1)} />;
  }

  return (
    <>
      {cars.length === 0 ? (
        <EmptyState />
      ) : (
        <ul className="lot">
          {cars.map((car, index) => (
            // The first card is the largest thing above the fold, so it is the page's
            // LCP element and should not be lazy-loaded.
            <CarCard key={car.id} car={car} priority={index === 0} />
          ))}
        </ul>
      )}

      {nextPage && (
        <button type="button" className="btn btn--quiet loadmore" onClick={loadMore}>
          {t("list.loadMore")}
        </button>
      )}

      <section className="ask" aria-labelledby="ask-title">
        <h2 id="ask-title" className="ask__title">{t("home.askTitle")}</h2>
        <p className="ask__body">{t("home.askBody")}</p>
        <Link className="callbtn" to="/request-a-car">{t("home.askAction")}</Link>
      </section>

      {sold.length > 0 && (
        <section className="lot-section" aria-labelledby="sold-title">
          <h2 id="sold-title" className="lot-section__head">{t("home.sold")}</h2>
          <p className="lot-section__body">{t("home.soldBody")}</p>
          <ul className="lot lot--sold">
            {sold.map((car) => (
              <CarCard key={car.id} car={car} />
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

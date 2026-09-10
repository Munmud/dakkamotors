import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchCars } from "../api/client";
import CarCard from "../components/CarCard";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { takeInitialData } from "../lib/initialData";

// Present on a fresh page load, absent after a client-side navigation.
const seeded = takeInitialData("home");

export default function Home() {
  const { t } = useTranslation();
  const [cars, setCars] = useState(seeded?.results ?? []);
  const [nextPage, setNextPage] = useState(seeded?.next ? 2 : null);
  const [status, setStatus] = useState(seeded ? "ready" : "loading");
  const [attempt, setAttempt] = useState(0);

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
  if (cars.length === 0) return <EmptyState />;

  return (
    <>
      <ul className="lot">
        {cars.map((car, index) => (
          // The first card is the largest thing above the fold, so it is the page's
          // LCP element and should not be lazy-loaded.
          <CarCard key={car.id} car={car} priority={index === 0} />
        ))}
      </ul>

      {nextPage && (
        <button type="button" className="btn btn--quiet loadmore" onClick={loadMore}>
          {t("list.loadMore")}
        </button>
      )}
    </>
  );
}

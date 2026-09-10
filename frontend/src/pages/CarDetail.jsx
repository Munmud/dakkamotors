import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { fetchCar } from "../api/client";
import CallButton from "../components/CallButton";
import CarVideo from "../components/CarVideo";
import Gallery from "../components/Gallery";
import SpecTable from "../components/SpecTable";
import { ErrorState, LoadingState } from "../components/States";
import { carTitle, formatPrice, hasPhone, pickDescription } from "../lib/format";
import { takeInitialData } from "../lib/initialData";

/**
 * Poster frame for the video player.
 *
 * Deliberately a resized copy rather than `image`: the original is the full-resolution
 * upload, several megabytes of it, and using it here would download the whole thing on
 * every visit to a car that has a video - undoing the point of preload="none".
 */
function videoPoster(car) {
  const primary = car.images?.find((image) => image.is_primary) ?? car.images?.[0];
  if (!primary) return undefined;
  return primary.sources?.["800"] ?? primary.image;
}

export default function CarDetail() {
  const { slug } = useParams();
  const { t, i18n } = useTranslation();
  // Only valid when the browser landed directly on this car's URL.
  const [seeded] = useState(() => takeInitialData("car", slug));
  const [car, setCar] = useState(seeded);
  const [status, setStatus] = useState(seeded ? "ready" : "loading");

  useEffect(() => {
    if (seeded && seeded.slug === slug) return undefined;

    const controller = new AbortController();
    setStatus("loading");
    window.scrollTo(0, 0);

    fetchCar(slug, { signal: controller.signal })
      .then((data) => {
        setCar(data);
        setStatus("ready");
      })
      .catch((error) => {
        if (error.name === "CanceledError") return;
        setStatus(error.response?.status === 404 ? "missing" : "error");
      });

    return () => controller.abort();
  }, [slug, seeded]);

  if (status === "loading") return <LoadingState />;

  if (status === "missing") {
    return (
      <>
        <Link className="backlink" to="/">
          {t("nav.back")}
        </Link>
        <ErrorState title={t("error.notFound")} body={t("error.notFoundBody")} />
      </>
    );
  }

  if (status === "error") {
    return <ErrorState onRetry={() => setStatus("loading")} />;
  }

  const price = formatPrice(car.price_jpy, i18n.language);
  const description = pickDescription(car, i18n.language);
  const title = carTitle(car);

  return (
    <>
      <Link className="backlink" to="/">
        {t("nav.back")}
      </Link>

      <div className="detail">
        <Gallery images={car.images} title={title} />

        <div className="summary">
          <div>
            <span className="summary__year u-nums">{car.manufacture_year}</span>
            <h1 className="summary__title">
              {car.brand} {car.model_name}
            </h1>
            {car.grade && <p className="summary__grade">{car.grade}</p>}
          </div>

          <div className="pricebox">
            {price ? (
              <span className="pricebox__amount u-nums">
                {t("price.yen", { amount: price })}
              </span>
            ) : (
              <span className="pricebox__amount pricebox__amount--call">
                {t("price.callFor")}
              </span>
            )}

            {car.status !== "available" && (
              <span className="card__flag" style={{ position: "static", justifySelf: "start" }}>
                {t(`status.${car.status}`)}
              </span>
            )}

            <CallButton />
            {car.status === "available" && (
              <Link className="btn bookbtn--outline" to={`/cars/${car.slug}/test-drive`}>
                {t("booking.book")}
              </Link>
            )}
            {hasPhone && <p className="pricebox__note">{t("call.hours")}</p>}
          </div>
        </div>
      </div>

      <CarVideo
        src={car.video}
        poster={videoPoster(car)}
        title={title}
      />

      <section className="section">
        <h2 className="section__title">{t("spec.heading")}</h2>
        <SpecTable car={car} />
      </section>

      <section className="section">
        <h2 className="section__title">{t("detail.about")}</h2>
        {description ? (
          <p className="prose">{description}</p>
        ) : (
          <p className="prose prose--muted">{t("detail.noDescription")}</p>
        )}
      </section>
    </>
  );
}

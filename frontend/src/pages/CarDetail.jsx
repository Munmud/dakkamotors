import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { fetchCar } from "../api/client";
import CarQuestions from "../components/CarQuestions";
import Gallery from "../components/Gallery";
import SpecTable from "../components/SpecTable";
import { ErrorState, LoadingState } from "../components/States";
import { carField, carTitle, formatPrice, pickDescription } from "../lib/format";
import { takeInitialData } from "../lib/initialData";
import { viewedCar } from "../lib/pixel";

/**
 * Poster frame for every video in the gallery, and the image behind their thumbnails.
 *
 * Deliberately a resized copy rather than `image`: the original is the full-resolution
 * upload, several megabytes of it, and using it here would download the whole thing on
 * every visit to a car that has a video - undoing the point of preload="none".
 *
 * Read from `images`, which is photos only, so this cannot accidentally be handed a
 * video and produce a poster that is the clip itself.
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
    const controller = new AbortController();
    const fresh = seeded && seeded.slug === slug;

    // Seeded: paint from the server's copy and refetch quietly behind it. The page
    // HTML is cached at the edge for up to fifteen minutes with the car's status
    // inside it, and a car reserved (or freed) in that window would otherwise stay
    // wrong until the cache turned over. The JSON is cached for one minute, so this
    // corrects the page within about that, and a staff save invalidates both anyway.
    if (!fresh) {
      setStatus("loading");
      window.scrollTo(0, 0);
    }

    fetchCar(slug, { signal: controller.signal })
      .then((data) => {
        setCar(data);
        setStatus("ready");
      })
      .catch((error) => {
        if (error.name === "CanceledError") return;
        // A quiet refetch that fails leaves the seeded page alone: the copy on
        // screen was good enough to render, and an error state over it would be a
        // regression for the sake of a refresh nobody asked for.
        if (fresh) return;
        setStatus(error.response?.status === 404 ? "missing" : "error");
      });

    return () => controller.abort();
  }, [slug, seeded]);

  // The car an advertisement was for. Everything the campaign reports is tied back to
  // this slug, so it is sent as soon as the page is on screen rather than waiting for
  // the refetch -- a visitor who reads the page and leaves still counts as having
  // looked at that car.
  useEffect(() => {
    viewedCar(slug);
  }, [slug]);

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
  const title = carTitle(car, i18n.language);

  return (
    <>
      <Link className="backlink" to="/">
        {t("nav.back")}
      </Link>

      <div className="detail">
        <Gallery
          media={car.media}
          images={car.images}
          poster={videoPoster(car)}
          title={title}
        />

        <div className="summary">
          <div>
            <span className="summary__year u-nums">{car.manufacture_year}</span>
            <h1 className="summary__title">
              {carField(car, "brand", i18n.language)} {carField(car, "model_name", i18n.language)}
            </h1>
            {car.grade && <p className="summary__grade">{car.grade}</p>}
          </div>

          <div className="pricebox">
            {/* A sold car with no price shows none. "Call for price" is an invitation
                to ring up about a number nobody can act on any more; the status flag
                below is the whole answer. The listing card already works this way. */}
            {price ? (
              <span className="pricebox__amount u-nums">
                {t("price.yen", { amount: price })}
              </span>
            ) : car.status === "sold" ? null : (
              <span className="pricebox__amount pricebox__amount--call">
                {t("price.callFor")}
              </span>
            )}

            {car.status !== "available" && (
              <span className="card__flag" style={{ position: "static", justifySelf: "start" }}>
                {t(`status.${car.status}`)}
              </span>
            )}

            {/*
              The page's one action, and so the one thing besides the price that takes
              the yellow. The call button used to sit here too; the phone number lives
              in the footer of every page, and two yellow buttons in one column meant
              neither led.
            */}
            {car.status === "available" && (
              <Link className="callbtn bookbtn" to={`/cars/${car.slug}/test-drive`}>
                {t("booking.book")}
              </Link>
            )}
          </div>
        </div>
      </div>

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

      <CarQuestions car={car} />
    </>
  );
}

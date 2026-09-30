import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { fetchCar } from "../api/client";
import Gallery from "../components/Gallery";
import LeadForm from "../components/LeadForm";
import SpecTable from "../components/SpecTable";
import { ErrorState, LoadingState } from "../components/States";
import { carField, carTitle, formatPrice, pickDescription } from "../lib/format";
import { takeInitialData } from "../lib/initialData";
import { usePageTitle } from "../lib/title";
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
  // Shared by both copies of the form on this page: once one is sent, the other
  // shows the confirmation rather than an empty form asking the same question.
  const [sent, setSent] = useState(false);

  // Nothing until the car has loaded -- an empty title would blank the tab for the
  // moment a cold navigation takes, which is worse than the previous page's.
  usePageTitle(car ? carTitle(car, i18n.language) : null, i18n.language);

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
          </div>

          <div className={`pricebox${car.status === "available" ? "" : " pricebox--gone"}`}>
            {/* A sold car with no price shows none. "Call for price" is an invitation
                to ring up about a number nobody can act on any more; the status flag
                below is the whole answer. The listing card already works this way. */}
            {price ? (
              /*
                The label above the number and the sentence below it, because the
                number alone is read as the bad case. In Japan a listed price is
                usually 車両本体価格 and the 諸費用 arrive at the desk, so a bare
                figure invites the suspicion that this one does too. It does not --
                this price is 支払総額 -- and that is the strongest thing this shop
                can say to somebody deciding whether to ring.
              */
              <>
                <span className="pricebox__label">{t("price.total")}</span>
                <span className="pricebox__amount u-nums">
                  {t("price.yen", { amount: price })}
                </span>
                <span className="pricebox__note">{t("price.totalNote")}</span>
              </>
            ) : car.status === "sold" ? null : (
              /* The plate at full size, and the sentence under it. Setting the
                 sentence *in* the plate shrank it to `--step-1`, so a car with no
                 price ended up with less visual weight than one with a price --
                 backwards, since it is the car you most need to ring about. */
              <>
                <span className="pricebox__amount">{t("price.onApplication")}</span>
                <span className="pricebox__note">{t("price.callFor")}</span>
              </>
            )}

            {car.status !== "available" && (
              <span className="pricebox__flag">{t(`status.${car.status}`)}</span>
            )}
          </div>

          {/*
            The form itself, beside the price, above everything else on the page.

            It used to be a link here — "Book a test drive" — which led to a calendar
            and, for a stranger, a sign-up wall. Nobody ever came out the other end.
            A form a visitor can see and fill without navigating is the difference
            between a page that asks for a decision and one that asks for a number.

            Only for a car somebody can still buy. On a sold car the shelf is a
            record, and inviting a call about it wastes both people's time.
          */}
          {car.status !== "sold" && (
            <LeadForm car={car.slug} id="ask" sent={sent} onSent={() => setSent(true)}
                      reserved={car.status === "reserved"} />
          )}
        </div>
      </div>

      <section className="section">
        <h2 className="section__title">{t("spec.heading")}</h2>
        <SpecTable car={car} />
      </section>

      {description && (
        <section className="section">
          <h2 className="section__title">{t("detail.about")}</h2>
          <p className="prose">{description}</p>
        </section>
      )}

      {/*
        Again at the bottom, for the reader who scrolled all the way through the specs
        and made up their mind there. Sending them back up to find the form is the
        kind of small friction that loses the ones who were nearly convinced.
      */}
      {car.status !== "sold" && (
        <section className="section">
          <h2 className="section__title">{t("lead.bottomHeading")}</h2>
          <p className="prose">{t("lead.bottomBody")}</p>
          <LeadForm car={car.slug} sent={sent} onSent={() => setSent(true)} bare
                    reserved={car.status === "reserved"} />
        </section>
      )}
    </>
  );
}

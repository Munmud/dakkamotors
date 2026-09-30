import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { carField, formatPrice } from "../lib/format";
import ResponsiveImage from "./ResponsiveImage";

// Every breakpoint the card has, in the order `30-listing.css` declares them. This is
// a description of that stylesheet and it has to be kept true by hand: the browser
// picks the file from `sizes` before any CSS has loaded, so a wrong number here is
// not corrected later, it is simply the wrong image.
//
//   <=559   stacked, full bleed
//   <=899   photo beside the name, `minmax(0, 20rem)`
//   <=1099  the same, widened to 24rem
//   >=1100  two cards abreast and each stacked again, so the photo is the card
//
// It asserted a flat 440px for a year, a number that was never in the stylesheet at
// all, and then stayed at 440 when the top breakpoint went back to stacking and the
// photo grew to ~530.
const CARD_SIZES = [
  "(max-width: 559px) 100vw",
  "(max-width: 899px) 320px",
  "(max-width: 1099px) 384px",
  "530px",
].join(", ");

export default function CarCard({ car, priority = false }) {
  const { t, i18n } = useTranslation();
  const price = formatPrice(car.price_jpy, i18n.language);
  const photo = car.primary_image?.image;
  const sold = car.status === "sold";
  const gone = car.status !== "available";
  const brand = carField(car, "brand", i18n.language);
  const model = carField(car, "model_name", i18n.language);

  // Every card carries its status now, not only the ones that are not for sale. A
  // lot with green markers on most cars and a grey one on the rest reads at a glance;
  // one with markers only on the exceptions asked the buyer to notice an absence.
  const flag = `card__flag card__flag--${car.status}`;

  return (
    <li>
      <Link
        className={`card${gone ? " card--gone" : ""}${sold ? " card--sold" : ""}`}
        to={`/cars/${car.slug ?? car.id}`}
      >
        <div className="card__frame">
          {photo ? (
            <ResponsiveImage
              className="card__photo"
              image={car.primary_image}
              sizes={CARD_SIZES}
              priority={priority}
            />
          ) : (
            <span className="card__nophoto">{t("detail.noPhotos")}</span>
          )}
          <span className={flag}>{t(`status.${car.status}`)}</span>

          {/* The price rides on the photograph, where a forecourt puts its windscreen
              card. It is inside the frame rather than under it so the two read as one
              object -- a number floating below a picture is a caption, not a price.
              A sold car shows none: a number nobody can act on is not a price. */}
          {sold ? null : price ? (
            <span className="card__plate u-nums">
              {t("price.yen", { amount: price })}
            </span>
          ) : (
            /* The plate takes the short form only. "価格はお問い合わせください" is
               twelve characters and stretched the yellow across the whole photo,
               which breaks the one device the visual system rests on -- the plate
               means "this is the price", and a sentence in it reads as a slogan. */
            <span className="card__plate card__plate--call">
              {t("price.onApplication")}
            </span>
          )}
        </div>

        <div className="card__body">
          <span className="card__year u-nums">{car.manufacture_year}</span>
          <h2 className="card__title">
            {brand} {model}
          </h2>
          {/* No grade. It rendered as a bare "FX" or "S" under the name, with
              nothing to say what it was -- a trim code means something to a dealer
              and nothing to the person this page is for. It is still on the car, and
              still in the spec table below, where it has a label. */}
        </div>
      </Link>
    </li>
  );
}

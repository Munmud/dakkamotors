import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { formatPrice } from "../lib/format";
import ResponsiveImage from "./ResponsiveImage";

// Full width on a phone, a little under half the shell once the row splits in two.
// The breakpoint has to be the one in 00-tokens.css; this asserted 700px for a year,
// a number that was never in the stylesheet at all, so the browser was picking the
// wrong copy on every tablet.
const CARD_SIZES = "(max-width: 559px) 100vw, 440px";

export default function CarCard({ car, priority = false }) {
  const { t, i18n } = useTranslation();
  const price = formatPrice(car.price_jpy, i18n.language);
  const photo = car.primary_image?.image;
  const gone = car.status !== "available";

  return (
    <li>
      <Link className={`card${gone ? " card--gone" : ""}`} to={`/cars/${car.slug ?? car.id}`}>
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
          {gone && <span className="card__flag">{t(`status.${car.status}`)}</span>}

          {/* The price rides on the photograph, where a forecourt puts its windscreen
              card. It is inside the frame rather than under it so the two read as one
              object -- a number floating below a picture is a caption, not a price. */}
          {price ? (
            <span className="card__plate u-nums">
              {t("price.yen", { amount: price })}
            </span>
          ) : (
            <span className="card__plate card__plate--call">{t("price.callFor")}</span>
          )}
        </div>

        <div className="card__body">
          <span className="card__year u-nums">{car.manufacture_year}</span>
          <h2 className="card__title">
            {car.brand} {car.model_name}
          </h2>
          {car.grade && <span className="card__grade">{car.grade}</span>}
        </div>
      </Link>
    </li>
  );
}

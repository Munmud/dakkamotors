import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { carField, formatPrice } from "../lib/format";
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
            <span className="card__plate card__plate--call">{t("price.callFor")}</span>
          )}
        </div>

        <div className="card__body">
          <span className="card__year u-nums">{car.manufacture_year}</span>
          <h2 className="card__title">
            {brand} {model}
          </h2>
          {car.grade && <span className="card__grade">{car.grade}</span>}
        </div>
      </Link>
    </li>
  );
}

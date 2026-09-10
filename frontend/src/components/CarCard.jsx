import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { formatPrice } from "../lib/format";
import ResponsiveImage from "./ResponsiveImage";

// One column on phones, roughly a quarter of the 1120px shell on desktop. Without this
// the browser assumes the image spans the viewport and downloads the largest copy.
const CARD_SIZES = "(max-width: 700px) 100vw, 300px";

export default function CarCard({ car, priority = false }) {
  const { t, i18n } = useTranslation();
  const price = formatPrice(car.price_jpy, i18n.language);
  const photo = car.primary_image?.image;

  return (
    <li>
      <Link className="card" to={`/cars/${car.slug ?? car.id}`}>
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
          {car.status !== "available" && (
            <span className="card__flag">{t(`status.${car.status}`)}</span>
          )}
        </div>

        <div className="card__body">
          <span className="card__year u-nums">{car.manufacture_year}</span>
          <h2 className="card__title">
            {car.brand} {car.model_name}
          </h2>
          {car.grade && <span className="card__grade">{car.grade}</span>}

          {price ? (
            <span className="card__price u-nums">{t("price.yen", { amount: price })}</span>
          ) : (
            <span className="card__price card__price--call">{t("price.callFor")}</span>
          )}
        </div>
      </Link>
    </li>
  );
}

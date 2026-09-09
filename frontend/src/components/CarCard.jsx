import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { formatPrice } from "../lib/format";

export default function CarCard({ car }) {
  const { t, i18n } = useTranslation();
  const price = formatPrice(car.price_jpy, i18n.language);
  const photo = car.primary_image?.image;

  return (
    <li>
      <Link className="card" to={`/cars/${car.id}`}>
        <div className="card__frame">
          {photo ? (
            <img
              className="card__photo"
              src={photo}
              alt=""
              loading="lazy"
              decoding="async"
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

import { useState } from "react";
import { useTranslation } from "react-i18next";

export default function Gallery({ images, title }) {
  const { t } = useTranslation();
  const [active, setActive] = useState(0);

  if (!images || images.length === 0) {
    return (
      <div>
        <div className="gallery__frame">
          <span className="gallery__empty">{t("detail.noPhotos")}</span>
        </div>
      </div>
    );
  }

  const current = images[Math.min(active, images.length - 1)];

  return (
    <div>
      <div className="gallery__frame">
        <img className="gallery__photo" src={current.image} alt={title} />
      </div>

      {images.length > 1 && (
        <ul className="gallery__thumbs">
          {images.map((image, index) => (
            <li key={image.id}>
              <button
                type="button"
                className="gallery__thumb"
                aria-current={index === active}
                aria-label={t("detail.gallery", {
                  index: index + 1,
                  total: images.length,
                })}
                onClick={() => setActive(index)}
              >
                <img src={image.image} alt="" loading="lazy" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

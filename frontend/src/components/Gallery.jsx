import { useState } from "react";
import { useTranslation } from "react-i18next";

import ResponsiveImage from "./ResponsiveImage";

// The gallery is the dominant column on desktop and full width on a phone.
const GALLERY_SIZES = "(max-width: 900px) 100vw, 700px";

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
        {/* The main photo is the detail page's LCP element. */}
        <ResponsiveImage
          className="gallery__photo"
          image={current}
          alt={title}
          sizes={GALLERY_SIZES}
          priority
        />
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
                {/* Rendered 74px wide, so the 320px copy is as large as it ever
                    needs to be - never the multi-megabyte original. */}
                <ResponsiveImage image={image} pinnedWidth={320} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

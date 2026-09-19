import { useState } from "react";
import { useTranslation } from "react-i18next";

import ResponsiveImage from "./ResponsiveImage";

// The gallery is the dominant column on desktop and full width on a phone.
const GALLERY_SIZES = "(max-width: 900px) 100vw, 700px";

/**
 * Photos and videos in the one order staff arranged, so a walkaround can lead.
 *
 * `media` is the list; `images` is only a fallback. A page that was server-rendered
 * before this shipped has the old payload embedded in it, and CloudFront will serve
 * that HTML for as long as its cache holds — so the component has to render from
 * either shape or a returning visitor gets an empty frame.
 *
 * A video's thumbnail is the car's primary photo with a play badge over it, not a
 * frame from the clip: nothing generates a poster, and asking the browser for one
 * would mean downloading the video to find it — exactly what `preload="none"` exists
 * to avoid.
 */
export default function Gallery({ media, images, poster, title }) {
  const { t } = useTranslation();
  const [active, setActive] = useState(0);

  const items = media?.length ? media : (images ?? []).map(asPhoto);

  if (items.length === 0) {
    return (
      <div>
        <div className="gallery__frame">
          <span className="gallery__empty">{t("detail.noPhotos")}</span>
        </div>
      </div>
    );
  }

  const current = items[Math.min(active, items.length - 1)];

  return (
    <div>
      <div className="gallery__frame">
        {current.kind === "video" ? (
          /* `key` is load-bearing: without it React reuses the element across a
             thumbnail click and the previous clip keeps playing under the new src. */
          <video
            key={current.id}
            className="gallery__video"
            src={current.video}
            poster={poster || undefined}
            controls
            preload="none"
            playsInline
            aria-label={title}
          >
            {t("detail.videoUnsupported")}
          </video>
        ) : (
          /* The main photo is the detail page's LCP element. */
          <ResponsiveImage
            className="gallery__photo"
            image={current}
            alt={title}
            sizes={GALLERY_SIZES}
            priority
          />
        )}
      </div>

      {items.length > 1 && (
        <ul className="gallery__thumbs">
          {items.map((item, index) => (
            <li key={`${item.kind}-${item.id}`}>
              <button
                type="button"
                className="gallery__thumb"
                aria-current={index === active}
                aria-label={t(
                  item.kind === "video" ? "detail.galleryVideo" : "detail.gallery",
                  { index: index + 1, total: items.length },
                )}
                onClick={() => setActive(index)}
              >
                {item.kind === "video" ? (
                  <span className="gallery__thumb-video">
                    {poster && <img src={poster} alt="" loading="lazy" decoding="async" />}
                    <span className="gallery__play" aria-hidden="true">
                      ▶
                    </span>
                  </span>
                ) : (
                  /* Rendered 74px wide, so the 320px copy is as large as it ever
                     needs to be - never the multi-megabyte original. */
                  <ResponsiveImage image={item} pinnedWidth={320} />
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** An old payload's `images` entry, which predates `kind`. */
function asPhoto(image) {
  return { ...image, kind: "photo" };
}

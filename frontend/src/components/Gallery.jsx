import { useCallback, useEffect, useRef, useState } from "react";
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
  const [open, setOpen] = useState(false);
  const closeButton = useRef(null);
  const opener = useRef(null);

  const items = media?.length ? media : (images ?? []).map(asPhoto);
  const count = items.length;

  const step = useCallback((by) => {
    setActive((i) => (i + by + count) % count);
  }, [count]);

  /*
    Escape closes, arrows move. Bound while the overlay is open and only then, so the
    arrow keys still belong to the page the rest of the time.

    The listener is on `document` rather than the overlay because focus starts on the
    close button and a viewer who has clicked the photograph itself would otherwise
    be pressing arrows at nothing.
  */
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => {
      if (event.key === "Escape") setOpen(false);
      else if (event.key === "ArrowRight") step(1);
      else if (event.key === "ArrowLeft") step(-1);
      else return;
      event.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    // The page behind must not scroll under the overlay; on iOS it otherwise takes
    // the drag and the photograph appears frozen.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
      // Focus goes back where it came from, or it lands on <body> and a keyboard
      // viewer restarts at the top of the document.
      opener.current?.focus();
    };
  }, [open, step]);

  if (items.length === 0) {
    return (
      <div className="gallery">
        <div className="gallery__frame">
          <span className="gallery__empty">{t("detail.noPhotos")}</span>
        </div>
      </div>
    );
  }

  const current = items[Math.min(active, items.length - 1)];

  // The root carries `gallery` so the detail grid can order it: on a phone the name,
  // the price and the form go above the photographs, and that is done with `order`
  // rather than by moving this in the markup.
  return (
    <div className="gallery">
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
          /*
            The main photo is the detail page's LCP element, and a button.

            It rendered 358x239 on a phone, which is not a size anybody decides to
            spend several hundred thousand yen from. The photographs are the product
            on a used-car page -- somebody wants to look at the sill, the seat wear,
            the kerbing on a wheel, which is why the shop uploaded eight of them --
            and the only way to see one properly was a long-press and "open image in
            new tab". The 1600px derivative already exists and was never reachable.

            A `button`, not an `a`: it opens an overlay in this page rather than
            navigating, and the frame is also cropped `object-fit: cover`, so the
            overlay is the only place the whole photograph is ever visible.
          */
          <button
            type="button"
            ref={opener}
            className="gallery__open"
            onClick={() => setOpen(true)}
            aria-label={t("detail.enlarge")}
          >
            <ResponsiveImage
              className="gallery__photo"
              image={current}
              alt={title}
              sizes={GALLERY_SIZES}
              priority
            />
            <span className="gallery__zoom" aria-hidden="true">⤢</span>
          </button>
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

      {open && current.kind !== "video" && (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label={title}>
          {/* The backdrop closes on click. It is a plain div rather than a button:
              it is a convenience for a pointer, and Escape and the close button are
              the routes out that have to work for everybody. */}
          <div className="lightbox__scrim" onClick={() => setOpen(false)} />

          <ResponsiveImage
            className="lightbox__photo"
            image={current}
            alt={title}
            sizes="100vw"
            priority
          />

          <button type="button" ref={closeButton} className="lightbox__close"
                  onClick={() => setOpen(false)} aria-label={t("detail.close")}>
            ✕
          </button>

          {count > 1 && (
            <>
              <button type="button" className="lightbox__step lightbox__step--prev"
                      onClick={() => step(-1)} aria-label={t("detail.previous")}>
                ‹
              </button>
              <button type="button" className="lightbox__step lightbox__step--next"
                      onClick={() => step(1)} aria-label={t("detail.next")}>
                ›
              </button>
              <p className="lightbox__count u-nums" aria-live="polite">
                {active + 1} / {count}
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** An old payload's `images` entry, which predates `kind`. */
function asPhoto(image) {
  return { ...image, kind: "photo" };
}

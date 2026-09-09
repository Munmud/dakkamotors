import { useTranslation } from "react-i18next";

/**
 * The walkaround clip.
 *
 * `preload="none"` is the whole point: without it a browser fetches metadata (and
 * Safari sometimes far more) on every page view, which would make adding video a
 * straight regression for anyone who never presses play. With it, a car page costs the
 * same whether or not it has a video attached.
 *
 * The poster is the car's own primary photo, so the player shows the vehicle rather
 * than a black rectangle - and costs nothing extra, since that image is already loaded
 * for the gallery.
 */
export default function CarVideo({ src, poster, title }) {
  const { t } = useTranslation();

  if (!src) return null;

  return (
    <section className="section">
      <h2 className="section__title">{t("detail.video")}</h2>
      <video
        className="carvideo"
        src={src}
        poster={poster || undefined}
        controls
        preload="none"
        playsInline
        aria-label={title}
      >
        {t("detail.videoUnsupported")}
      </video>
    </section>
  );
}

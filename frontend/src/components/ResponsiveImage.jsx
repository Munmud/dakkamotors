/**
 * One car photo, served at a size that suits the screen showing it.
 *
 * The API returns `sources` as {width: webpUrl} once the resized copies exist, and
 * `null` while they are still being generated. When it is null this falls back to a
 * plain <img> on the original: a <source> pointing at an object that does not exist
 * yet would render as a broken image rather than degrading.
 *
 * **Every image carries `width` and `height`.** Not the photograph's real pixels — a
 * 3:2 ratio, which is what the frames are. The browser reserves the box from the
 * attributes before a single byte of image arrives, which is what stops the page
 * jumping as photos land. The frames also set `aspect-ratio` in CSS and that was the
 * only defence until now; it measured **0.226 CLS** on the live car page, where
 * anything over 0.1 is a failure. CSS arrives one request later than the markup, and
 * on a slow connection that gap is the shift.
 *
 * **The `<img>` fallback is a derivative, never the original.** It used to be
 * `image.image` — the raw upload, up to 15 MB — which every browser without WebP
 * support downloaded, and which *every* browser downloaded whenever `sources` was
 * null because the resize budget had run out. A 1600px WebP is the widest thing this
 * site should ever hand anybody.
 */

/** The frames are all 3:2. One number, so the attributes and the CSS cannot disagree. */
const RATIO_W = 3;
const RATIO_H = 2;

function widest(sources) {
  const widths = Object.keys(sources).map(Number).sort((a, b) => a - b);
  return sources[String(widths[widths.length - 1])];
}

export default function ResponsiveImage({
  image,
  alt = "",
  sizes,
  className,
  priority = false,
  pinnedWidth,
}) {
  const original = image?.image;
  if (!original) return null;

  const sources = image.sources;

  // Thumbnails are always displayed small, so there is nothing to choose between
  // at render time - pin them to the narrowest copy and skip srcset entirely.
  if (pinnedWidth && sources) {
    const available = Object.keys(sources)
      .map(Number)
      .sort((a, b) => a - b);
    const chosen = available.find((w) => w >= pinnedWidth) ?? available[available.length - 1];
    return (
      <img
        className={className}
        src={sources[String(chosen)]}
        alt={alt}
        width={pinnedWidth}
        height={Math.round((pinnedWidth * RATIO_H) / RATIO_W)}
        loading="lazy"
        decoding="async"
      />
    );
  }

  // 1200 is a display width, not a file: it only sets the attribute ratio. `sizes`
  // is what actually picks the file.
  const width = 1200;
  const img = (
    <img
      className={className}
      src={sources ? widest(sources) : original}
      alt={alt}
      width={width}
      height={Math.round((width * RATIO_H) / RATIO_W)}
      sizes={sources ? sizes : undefined}
      loading={priority ? "eager" : "lazy"}
      decoding={priority ? "sync" : "async"}
      fetchPriority={priority ? "high" : undefined}
    />
  );

  if (!sources) return img;

  const srcSet = Object.entries(sources)
    .map(([w, url]) => `${url} ${w}w`)
    .join(", ");

  return (
    <picture>
      <source type="image/webp" srcSet={srcSet} sizes={sizes} />
      {img}
    </picture>
  );
}

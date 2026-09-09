/**
 * One car photo, served at a size that suits the screen showing it.
 *
 * The API returns `sources` as {width: webpUrl} once the resized copies exist, and
 * `null` while they are still being generated. When it is null this falls back to a
 * plain <img> on the original: a <source> pointing at an object that does not exist
 * yet would render as a broken image rather than degrading.
 */
export default function ResponsiveImage({
  image,
  alt = "",
  sizes,
  className,
  priority = false,
  pinnedWidth,
}) {
  const fallback = image?.image;
  if (!fallback) return null;

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
        loading="lazy"
        decoding="async"
      />
    );
  }

  const img = (
    <img
      className={className}
      src={fallback}
      alt={alt}
      sizes={sources ? sizes : undefined}
      loading={priority ? "eager" : "lazy"}
      decoding={priority ? "sync" : "async"}
      fetchPriority={priority ? "high" : undefined}
    />
  );

  if (!sources) return img;

  const srcSet = Object.entries(sources)
    .map(([width, url]) => `${url} ${width}w`)
    .join(", ");

  return (
    <picture>
      <source type="image/webp" srcSet={srcSet} sizes={sizes} />
      {img}
    </picture>
  );
}

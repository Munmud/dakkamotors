/**
 * Data the server already had, handed to the first render.
 *
 * Django renders the page with the same payload the API would return, embedded as a
 * JSON script block. Using it means the first paint shows real content instead of a
 * "Loading…" line that reflows into the full page a moment later — a reflow Lighthouse
 * measured at 0.309 cumulative layout shift, well inside its "poor" band. It also saves
 * a round-trip on the very first view, which is the one that matters.
 *
 * Consumed once. A client-side navigation to a different car must fetch properly rather
 * than reuse whatever the page happened to load with.
 */

let payload = null;

try {
  const node = document.getElementById("initial-data");
  if (node?.textContent) payload = JSON.parse(node.textContent);
} catch {
  // Malformed or absent: fall back to fetching, which always works.
  payload = null;
}

/**
 * @param kind "home" or "car"
 * @param key  for "car", the slug it must match; ignored otherwise
 */
export function takeInitialData(kind, key) {
  if (!payload || payload.kind !== kind) return null;
  if (key !== undefined && payload.key !== key) return null;

  const { data } = payload;
  payload = null;
  return data ?? null;
}

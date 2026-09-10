/**
 * Fail the build if en.json and ja.json have drifted apart.
 *
 * i18next is configured with `fallbackLng: "en"`, so a key missing from ja.json does not
 * throw, warn, or render a placeholder — it quietly serves English. Half the customers
 * read Japanese, and nobody would notice until one of them did.
 *
 * There is no JS test runner in this project and one is not worth adding for this, so
 * this runs in CI ahead of the build.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const load = (lang) =>
  JSON.parse(readFileSync(join(here, "..", "src", "i18n", `${lang}.json`), "utf8"));

/** Flatten to dotted paths, so a section present but empty is still a difference. */
function keysOf(value, prefix = "") {
  return Object.entries(value).flatMap(([key, inner]) =>
    inner && typeof inner === "object" && !Array.isArray(inner)
      ? keysOf(inner, `${prefix}${key}.`)
      : [`${prefix}${key}`],
  );
}

const en = new Set(keysOf(load("en")));
const ja = new Set(keysOf(load("ja")));

const missingFromJa = [...en].filter((key) => !ja.has(key)).sort();
const missingFromEn = [...ja].filter((key) => !en.has(key)).sort();

if (missingFromJa.length || missingFromEn.length) {
  if (missingFromJa.length) {
    console.error(`Missing from ja.json (${missingFromJa.length}):`);
    for (const key of missingFromJa) console.error(`  ${key}`);
  }
  if (missingFromEn.length) {
    console.error(`Missing from en.json (${missingFromEn.length}):`);
    for (const key of missingFromEn) console.error(`  ${key}`);
  }
  process.exit(1);
}

console.log(`i18n: ${en.size} keys, en and ja match.`);

/**
 * Fail the build if en.json and ja.json have drifted apart, and warn about keys no
 * component uses.
 *
 * i18next is configured with `fallbackLng: "en"`, so a key missing from ja.json does not
 * throw, warn, or render a placeholder — it quietly serves English. Half the customers
 * read Japanese, and nobody would notice until one of them did.
 *
 * Comparing the two files only against each other cannot catch a key both of them have
 * and nothing reads: seven accumulated that way before anyone looked. So the second pass
 * greps the source for each key. It **warns rather than fails**, because the detection is
 * necessarily approximate — keys reached through a template literal, `t(`status.${x}`)`,
 * cannot be resolved statically — and a check that cries wolf gets disabled. Prefixes
 * used that way are listed below and skipped.
 *
 * There is no JS test runner in this project and one is not worth adding for this, so
 * this runs in CI ahead of the build.
 */

import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, extname } from "node:path";

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

/** Sections reached as `t(`prefix.${variable}`)`, which no static check can resolve. */
const DYNAMIC = ["status", "language", "notifications", "spec"];

function sourceFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(full);
    return [".js", ".jsx"].includes(extname(entry.name)) ? [full] : [];
  });
}

const src = join(here, "..", "src");
const haystack = sourceFiles(src)
  .map((file) => readFileSync(file, "utf8"))
  .join(String.fromCharCode(10));

/** i18next appends these; a component asks for the bare key and i18next picks one. */
const PLURAL = /_(zero|one|two|few|many|other)$/;

const unused = [...en]
  .filter((key) => !DYNAMIC.includes(key.split(".")[0]))
  .filter((key) => !haystack.includes(key.replace(PLURAL, "")))
  .sort();

if (unused.length) {
  console.warn(`i18n: ${unused.length} key(s) no component references:`);
  for (const key of unused) console.warn(`  ${key}`);
  console.warn("  Remove them, or add the prefix to DYNAMIC if reached dynamically.");
}

console.log(`i18n: ${en.size} keys, en and ja match.`);

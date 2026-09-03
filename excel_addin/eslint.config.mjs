/* eslint.config.mjs — the dead-handler gate.
 *
 * A copy of assets/eslint.config.mjs from the excel-addin-development skill,
 * plus `module` (lib.js sets module.exports when node requires it).
 *
 * The rule that earns its place is no-undef. reloadThisSheet was deleted on
 * 2026-08-20 while its click handler kept calling it: a live ReferenceError,
 * thrown only on click, that shipped for two weeks because there was no build
 * step, no test and no watchdog to see it. no-undef names the exact line.
 */
export default [{
  files: ["**/*.js"],
  languageOptions: { ecmaVersion: 2022, sourceType: "script",
    globals: { window:"readonly", document:"readonly", console:"readonly", fetch:"readonly", localStorage:"readonly", sessionStorage:"readonly", setTimeout:"readonly", clearTimeout:"readonly", setInterval:"readonly", clearInterval:"readonly", URL:"readonly", URLSearchParams:"readonly", Blob:"readonly", FileReader:"readonly", navigator:"readonly", location:"readonly", alert:"readonly", confirm:"readonly", prompt:"readonly", requestAnimationFrame:"readonly", Event:"readonly", CustomEvent:"readonly", Promise:"readonly", Map:"readonly", Set:"readonly", JSON:"readonly", Math:"readonly", Date:"readonly", Intl:"readonly", module:"readonly", Office:"readonly", Excel:"readonly", OfficeExtension:"readonly", OfficeRuntime:"readonly", CustomFunctions:"readonly", crypto:"readonly", performance:"readonly", getComputedStyle:"readonly", HTMLElement:"readonly", Node:"readonly", NodeList:"readonly", Element:"readonly", AbortController:"readonly", TextEncoder:"readonly", TextDecoder:"readonly", atob:"readonly", btoa:"readonly", structuredClone:"readonly", queueMicrotask:"readonly", globalThis:"readonly", self:"readonly", MutationObserver:"readonly", ResizeObserver:"readonly", IntersectionObserver:"readonly", DOMParser:"readonly", XMLSerializer:"readonly", history:"readonly", screen:"readonly", open:"readonly", close:"readonly", Image:"readonly", FormData:"readonly", Headers:"readonly", Request:"readonly", Response:"readonly" } },
  rules: { "no-undef": "error",
    /* caughtErrors none: this file catches deliberately and ignores the
       error in ~25 places ("host without merge", "older host"), and 25
       standing warnings would hide the one that matters. */
    "no-unused-vars": ["warn", { "caughtErrors": "none" }] }
}];

// Diagrams for the PUBLIC surface — the page-level entry `viewer.html` loads
// only when the document carries a ```mermaid fence. The private shell imports
// `diagrams.js` directly; this file exists so the public page has a script
// URL to name, the same split as `pdf-visitor.js` over `pdfview.js`.
import { renderDiagrams, followTheme } from "./diagrams.js"; // module-relative

const main = document.querySelector("main.doc");
if (main) {
  await renderDiagrams(main);
  followTheme(main);
}

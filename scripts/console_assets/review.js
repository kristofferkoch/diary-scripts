"use strict";

function highlightSource(element) {
  const language = element.dataset.syntax;
  if (!language || !window.hljs || language === "plaintext") return;
  if (!window.hljs.getLanguage(language)) return;
  element.innerHTML = window.hljs.highlight(element.textContent, {language}).value;
}

document.querySelectorAll("[data-syntax]").forEach(highlightSource);

const markdownSource = document.getElementById("markdown-source");
const markdownTarget = document.getElementById("markdown-rendered");
if (markdownSource && markdownTarget && window.marked && window.DOMPurify) {
  const rendered = window.marked.parse(markdownSource.textContent, {
    gfm: true,
    breaks: false,
  });
  markdownTarget.innerHTML = window.DOMPurify.sanitize(rendered, {
    USE_PROFILES: {html: true},
    FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed", "svg", "math"],
    FORBID_ATTR: ["style"],
  });
  markdownTarget.querySelectorAll("a").forEach((link) => {
    link.rel = "noreferrer nofollow";
  });
  markdownTarget.querySelectorAll("pre code").forEach((block) => {
    const language = Array.from(block.classList)
      .find((name) => name.startsWith("language-"))
      ?.slice("language-".length);
    if (language && window.hljs?.getLanguage(language)) {
      block.dataset.syntax = language;
      highlightSource(block);
    }
  });
  markdownSource.hidden = true;
  markdownTarget.hidden = false;
}

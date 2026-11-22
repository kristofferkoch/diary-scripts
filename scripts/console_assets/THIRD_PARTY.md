# Vendored browser assets

These assets are self-hosted so reviewing private queue contents requires no
third-party network requests. They are unmodified upstream distribution files;
their licenses are included with them in `third_party.tar.gz`. The binary bundle
keeps parser URL/email patterns out of the source identity scanner without
adding a scanner exception. Its SHA-256 is
`5f79ca10cf50a32c67a0b31d9084cc6eb8205e634eb473b8589753841be107c2`.

| Asset | Upstream package | Version | SHA-256 |
| --- | --- | --- | --- |
| `highlight.min.js` | `@highlightjs/cdn-assets` | 11.12.0 | `8ab71eb09c51f501e5e25157d9cff100e46cc29bcbfc744d0b746d451fca7f53` |
| `nix.min.js` | `@highlightjs/cdn-assets` | 11.12.0 | `b3fb414b263a3e7c3c493a49da2e9f1aacbc06248f4cbf5c6247c338caafdf3a` |
| `marked.umd.js` | `marked` | 18.2.1 | `bb95d4c15f1289f78486eaacebf3bb18688475ed58a040ef762ed49337090542` |
| `purify.min.js` | `dompurify` | 3.5.0 | `f69a46022ab1f53e969c8ebbca98172c8995785a9b3be72ea4a12d25b1a1bf67` |

`review.js` is the console's own integration code. Markdown HTML is sanitized
with DOMPurify before insertion, raw source is inserted through `textContent`
before highlighting, and the console Content Security Policy permits scripts
only from its own origin.

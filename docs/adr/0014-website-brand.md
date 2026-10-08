# ADR 0014: Portal follows the www.exacarib.com brand

Date: 2026-10-04. Status: accepted.

## Context

The portal followed the brand brief of 30 September: teal `#00ABB6`, navy
`#011F4D`, a grey page, Lexend headings and IBM Plex Sans body text. On
4 October Dudley published the new ExaCarib website, which uses a royal blue
accent, system fonts and a white page with pale bands. It also names the
network product "ExaCarib Connect" and links "Connect Login" to
connect.exacarib.com. A customer moving from the site to the portal saw two
different brands.

The values were read from the site's own CSS (notes in
`/mnt/project-files/exaconnect/brand-exacarib-com.md`) and checked against
Dudley's screenshots.

## Decision

- Brand blue `#155EEF` replaces teal for buttons, links, eyebrows and marks
  (5.4:1 on white, so it is safe for text). Ink `#10213D`, navy `#07182E`,
  muted `#52647A`, line `#DFE6EE`, page `#F3F6FB`.
- System sans for headings and body, as on the site. IBM Plex Mono stays for
  figures, because tables and charts need fixed-width numbers.
- Buttons are solid blue with 5 px corners; cards keep 8 px corners and gain
  a 1 px line border instead of a shadow.
- The logo is the site's two-tone X, recoloured from the site's source file
  to blue and navy, with an all-white version for the sidebar.
- The interface says "Connect", not "ExaConnect". Code, the SDK package and
  repository names keep `exaconnect`.
- Kept: coral for Storm Mode and the satellite path, the status colours and
  the dark theme. The website has no equivalent, and they carry meaning.
- CSS tokens `--teal*` were renamed `--blue*`.

## Consequences

CLAUDE.md §4.6 now describes this brand. The logo PNGs are recoloured
rasters; if ExaCarib produces vector logo files, swap them in
`portal/public/brand/`.

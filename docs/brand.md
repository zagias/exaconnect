# ExaCarib brand in the portal

Source: *ExaCarib brand brief for product builds* v1.0 (30 Sep 2026), shared by
Dudley in the project (not committed here), and CLAUDE.md §4.6. If the brief
and the brand system disagree, the brand system wins.

Where it lives in code:

* `portal/src/brand.css`: every colour, spacing, radius, shadow and font token
  from the brief, light and dark themes, focus ring, eyebrow, status pill
  marks (round within SLA, diamond at risk, square down), cards and the 64px
  navy top bar.
* `portal/src/components.tsx`: `StatusPill`, `Eyebrow`, `ExampleTag`,
  `StatTile`, matching the brand system's components of the same names.
* `portal/public/brand/`: wordmark, reversed wordmark and symbol. These were
  taken from the images inside the brand brief PDF as stand-ins; replace them
  with the original `exacarib-wordmark-reversed.png`, `exacarib-wordmark.png`
  and `exacarib-symbol.png` files when available.

Rules to keep while building screens: grey page with white cards; teal is a
fill, teal-strong is text; status always has a word and a mark; coral only for
Storm Mode and the satellite path; figures in IBM Plex Mono with units; paths
named "Carrier A", "Carrier B", "Satellite"; "Example data" on any sample data;
British and Caribbean spelling; no tunnel/underlay/BFD words on customer screens.

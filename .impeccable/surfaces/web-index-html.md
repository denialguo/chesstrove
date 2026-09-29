---
version: 1
slug: "web-index-html"
primary_target: "web/index.html"
related_targets: ["web/src"]
---

# ChessTrove web app

Scope: landing (Persuade), player page (Operate: explore one player's history), game viewer (Operate).
Audience: any Chess.com/Lichess player arriving cold with a username. Job: see the rare moments in
their own games and verify them. Constraints: React + Vite, no login, free hosting, real data only
(sample finds on the landing are labeled as samples).

## Direction contract

THESIS: Every player page is a two-faced tournament clock: your side and their side of your history.
It refuses the dark chess-app dashboard (search bar over a board, stat tiles, green accent).

OWN-WORLD: Flat walnut field (#4A2C1D) as the case; white enamel dials (#F6F5F0) with minute ticks and
dial-black numerals (#16171A); brass (#B28A3E) only on controls, like plunger buttons; flag red (#C8222C)
only for moments that went wrong. No wood or brass textures. Counts sit in fixed tabular digit banks.
Big Shoulders Display numerals, Hanken Grotesk text, Martian Mono notation.

STORY: Type a username, press the brass plunger, watch the history tick in, then read the two faces:
finds you made on the left, finds made against you on the right. Any find opens its board and the
original game.

FIRST VIEWPORT: Full-bleed walnut. Centered clock body holding two large dials. Left dial: the
username field written on the face, with a Chess.com / Lichess toggle beneath. Right dial: a real
labeled sample find with its board. The brass plunger on top of the left dial is the primary action,
"Open my games". One line of copy above the clock.

FORM: Tournament Clock, #3 on the ordered list, seed key df15dcf9. Raised: fixed digit positions for
every count (from Nixie Counter); hierarchy by scale contrast alone (from Variable-Font Specimen).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

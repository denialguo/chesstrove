---
name: ChessTrove
description: Every game a player has played, searched for the moves worth keeping, read off a two-faced tournament clock.
colors:
  walnut: "#4a2c1d"
  walnut-deep: "#3b2216"
  walnut-edge: "#2c180e"
  walnut-rule: "#6a4633"
  on-walnut: "#f6f5f0"
  on-walnut-2: "#e2cfbd"
  on-walnut-3: "#c5a78f"
  enamel: "#f6f5f0"
  enamel-shade: "#e7e3d7"
  dial: "#16171a"
  dial-2: "#55534e"
  brass: "#b28a3e"
  brass-hi: "#d2ac5f"
  brass-lo: "#7e5f25"
  flag: "#c8222c"
  flag-soft: "#e8646b"
  sq-dark: "#b39277"
typography:
  display:
    fontFamily: "Source Serif 4 Variable, Georgia, serif"
    fontSize: "clamp(2.4rem, 5.5vw, 4rem)"
    fontWeight: 600
    lineHeight: 1.05
    letterSpacing: "-0.015em"
  headline:
    fontFamily: "Source Serif 4 Variable, Georgia, serif"
    fontSize: "1.6rem"
    fontWeight: 600
    lineHeight: 1.2
  count:
    fontFamily: "Source Serif 4 Variable, Georgia, serif"
    fontSize: "clamp(3rem, 22cqi, 7.5rem)"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontFeature: "\"tnum\", \"lnum\""
  title:
    fontFamily: "Source Sans 3 Variable, system-ui, sans-serif"
    fontSize: "1.1rem"
    fontWeight: 700
  body:
    fontFamily: "Source Sans 3 Variable, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Source Sans 3 Variable, system-ui, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 600
    letterSpacing: "0.02em"
  notation:
    fontFamily: "Source Code Pro Variable, ui-monospace, monospace"
    fontSize: "0.85rem"
    fontWeight: 600
rounded:
  board: "4px"
  row: "6px"
  control: "8px"
  plunger-top: "0.7rem"
  plunger-base: "0.25rem"
  case-bottom: "1.4rem"
  case-top: "2.2rem"
  pill: "999px"
spacing:
  space-1: "0.25rem"
  space-2: "0.5rem"
  space-3: "0.75rem"
  space-4: "1rem"
  space-5: "1.5rem"
  space-6: "2.25rem"
  space-7: "3.5rem"
  space-8: "5.5rem"
components:
  button-plunger:
    backgroundColor: "{colors.brass}"
    textColor: "{colors.dial}"
    typography: "{typography.headline}"
    padding: "0.85rem 1.6rem 0.75rem"
  button-plunger-hover:
    backgroundColor: "{colors.brass-hi}"
  button-go:
    backgroundColor: "{colors.brass}"
    textColor: "{colors.dial}"
    rounded: "{rounded.pill}"
    size: "2.2rem"
  button-go-hover:
    backgroundColor: "{colors.brass-hi}"
  button-transport:
    backgroundColor: "{colors.walnut-deep}"
    textColor: "{colors.on-walnut}"
    rounded: "{rounded.control}"
    size: "2.6rem"
  button-transport-hover:
    backgroundColor: "{colors.walnut-rule}"
  toggle-dial-selected:
    backgroundColor: "{colors.dial}"
    textColor: "{colors.enamel}"
    rounded: "{rounded.pill}"
    typography: "{typography.label}"
    padding: "0.3rem 0.8rem"
  toggle-bar-selected:
    backgroundColor: "{colors.walnut-rule}"
    textColor: "{colors.on-walnut}"
    rounded: "{rounded.pill}"
    typography: "{typography.label}"
    padding: "0.3rem 0.8rem"
  tab-selected:
    backgroundColor: "{colors.enamel}"
    textColor: "{colors.dial}"
    rounded: "{rounded.pill}"
    padding: "0.35rem 0.9rem"
  lookup-bar:
    backgroundColor: "{colors.walnut-deep}"
    textColor: "{colors.on-walnut}"
    rounded: "{rounded.pill}"
    padding: "0.3rem 0.35rem 0.3rem 1rem"
  move-current:
    backgroundColor: "{colors.enamel}"
    textColor: "{colors.dial}"
    typography: "{typography.notation}"
    height: "2.25rem"
  plate-rating-to-move:
    backgroundColor: "{colors.enamel}"
    textColor: "{colors.dial}"
    rounded: "{rounded.row}"
    padding: "0.2rem 0.45rem 0.15rem"
  plate-rating-waiting:
    backgroundColor: "{colors.walnut-deep}"
    textColor: "{colors.on-walnut-2}"
    rounded: "{rounded.row}"
    padding: "0.2rem 0.45rem 0.15rem"
---

# Design System: ChessTrove

## Overview

**Creative North Star: "The Tournament Clock"**

Every player page is a two-faced tournament clock: the player's side and their opponents' side of the same history. The page is the clock's case, a flat walnut field; the readouts are white enamel dials with sixty minute ticks and dial-black numerals; brass appears where a hand would press, like a clock's plunger buttons. Counts never float as free numbers; they sit in fixed-width digit banks, leading zeros kept and dimmed, so a page of counts reads like a bank of meters.

Hierarchy comes from serif names, headings and clock numerals, set like a chess book, against a plain sans for labels and explanations. There is no second accent, no gradient chrome, no card grid. Density is calm on the landing (one clock, a short invitation and a real game caption) and ledger-like on the player page (ruled rows with counts in the margins, like a scorebook). The reference this system turns away from is the dark chess-app dashboard: a search bar over a board, stat tiles, green accent.

Motion is mechanical and sparse: a second hand sweeps (6s per turn) only while something is running, the plunger sinks into the case when pressed, boards lift two pixels on hover. All of it stops under reduced motion.

**Key Characteristics:**
- Flat walnut case, white enamel dials with minute ticks, no wood or brass textures.
- Brass on controls and the player's own finds; flag red on moments that went wrong.
- Counts in fixed tabular digit banks with dimmed leading zeros.
- One type family, Adobe's Source: Serif 4 for names, headings and counts, Sans 3 for labels and text, Code Pro for notation.
- A sweeping hand means "running"; a still dial means "settled".

## Colors

A warm, dark wood field with cream enamel readouts and two narrowly-rationed signal colours: brass and flag red.

### Primary
- **Plunger Brass** (brass): the fill of every pressable brass part: the "Open my games" plunger, the round go button, the dial's progress track, and the preferred-move arrow on boards.
- **Polished Brass** (brass-hi): hover state of brass controls, focus rings, text caret, text links, and the player's own find counts in the ledger.
- **Tarnished Brass** (brass-lo): brass on an enamel (selected) cell, where brass-hi would wash out.

### Secondary
- **Flag Red** (flag): the fallen flag. The check glow on a king's square, the played-move arrow on a blunder, the error line on the dial form, and the legend key for "what was played".
- **Soft Flag** (flag-soft): flag red on walnut, where full flag loses contrast: the evaluation a blunder or missed win fell to, the "?" and "??" marks, form errors in the top bar.

### Neutral
- **Walnut** (walnut): the page itself, edge to edge; the case's field.
- **Deep Walnut** (walnut-deep): the clock case body, the lookup bar, transport buttons, segmented tracks, a waiting side's rating plate.
- **Walnut Edge** (walnut-edge): the hairline ring around small boards.
- **Walnut Rule** (walnut-rule): every divider and ledger rule, the selected platform in the top bar, transport hover.
- **Case Cream** (on-walnut): primary text on walnut.
- **Warm Linen** (on-walnut-2): secondary text on walnut: meta lines, definitions, the opponent-side counts.
- **Faded Linen** (on-walnut-3): tertiary text: column heads, move numbers, captions, empty motif rows.
- **White Enamel** (enamel): dial faces, the current move, selected tabs, the side-to-move rating plate, light squares.
- **Enamel Shade** (enamel-shade): the platform toggle track written on the dial face.
- **Dial Black** (dial): dial bezels, hour ticks, hands, numerals and text on enamel.
- **Dial Grey** (dial-2): minute ticks and labels on the dial face.
- **Boxwood** (sq-dark): dark squares of every board.

### Named Rules
**The Plunger Rule.** Brass is only for controls and the player's own finds. If a thing cannot be pressed and was not found by this player, it is not brass.

**The Fallen Flag Rule.** Flag red is only for moments that went wrong: checks, blunders, missed wins, errors. It never decorates.

**The Closed Palette Rule.** No colour outside these tokens. Hover washes are white at 3.5 to 4% over walnut; the last-move square is brass-hi at 55%; nothing else is mixed in.

## Typography

**Display Font:** Source Serif 4 Variable (with Georgia), optical sizes on, weight 600
**Body Font:** Source Sans 3 Variable (with system-ui)
**Label/Mono Font:** Source Code Pro Variable (with ui-monospace), weights 400 and 600

**Character:** One superfamily designed to work together, so the three voices share proportions instead of competing. The serif gives names, headings and numerals the feel of a chess book or tournament bulletin; the sans stays out of the way; the mono sets notation. Nothing is uppercased.

### Hierarchy
- **Display** (serif 600, clamp(2.4rem, 5.5vw, 4rem), 1.05, as written): the player's name and the game title; the page's single loudest line.
- **Headline** (serif 600, 1.6rem, 1.2, sentence case): section heads ("Found in these games", "Mating patterns") and the landing title (clock-sized, so the clock stays the hero). The game page's "Patterns in this game" uses 1.25rem.
- **Count** (serif 600, clamp(3rem, 22cqi, 7.5rem), 1, tabular lining figures): digit banks on dial faces, scaled to the dial via container units; ledger counts at 2rem, rating plates at 1.2rem.
- **Title** (700, 1.1rem): motif names in the ledger, engine list heads (1.2rem), player names on plates.
- **Body** (400, 1.0625rem, 1.55; 1rem on phones): explanations, capped at 68ch, definitions at 0.92rem.
- **Label** (600, 0.78 to 0.85rem, 0.02em): form labels, platform toggles, tabs, column heads, dial captions. Sentence case, never uppercase.
- **Notation** (600, 0.85rem): moves (29…Nf2#), motif marks (N#, =Q#, ??), the whole move list.

### Named Rules
**The Three Voices Rule.** Source Serif 4 is for names, headings and counts. Source Sans 3 is for buttons, labels and text. Source Code Pro is only for notation and glyphs; it never sets a sentence or a classification label.

**The Type Hierarchy Rule.** Names, headings and readouts carry the serif; navigation and explanations use the quieter sans. Do not manufacture emphasis with colour chips or boxes around headings.

## Layout

A single centred column, `min(100% - 2rem, 76rem)`, shared by the top bar, main and footer. Spacing runs on an expanding 8-step scale (space-1 to space-8, 0.25rem to 5.5rem). Related content sits close: the player ledger follows the clock by 2.25rem; the optional engine layer gets a 3.5rem break and a rule. Text is capped at a 68ch measure.

The clock case is the recurring composition: a rounded walnut-deep body with plungers riding its top edge. The landing holds two enamel dials, centred (max 46rem). Below 640px they stack, with a smaller sample-board dial and no decorative second plunger. The player case is left-aligned (max 60rem), with two small readouts beside their captions and examples; these groups stack on phones. A short counting explanation and W/D/L share the lower plate where space permits.

The player ledger is a three-column scorebook: the player's count left (5.5rem), motif in the middle, the opponents' count right; rows are separated by walnut rules. The game viewer is board (up to 36rem) plus a side column (move list, found list); it collapses to one column below 860px. The top-bar lookup reflows into two rows below 860px.

## Elevation & Depth

Surfaces are flat; depth is a physical object sitting on a table, not a UI layer stack. Only three things lift: the clock case, the plungers, and boards. Everything else, including dials, rows and bars, is flat colour.

### Shadow Vocabulary
- **Case** (`box-shadow: 0 1px 0 rgb(255 255 255 / 0.06) inset, 0 22px 40px -18px rgb(0 0 0 / 0.55), 0 2px 6px rgb(0 0 0 / 0.25)`): the clock body resting on the walnut, with a hairline top edge.
- **Plunger** (`box-shadow: 0 8px 14px -6px rgb(0 0 0 / 0.5)`, pressed/down `0 3px 6px -3px rgb(0 0 0 / 0.5)`): a button standing proud of the case, shallower when pushed in.
- **Specimen board** (`box-shadow: 0 0 0 1px var(--walnut-edge), 0 8px 16px -10px rgb(0 0 0 / 0.6)`), hover swaps the ring to 2px brass and lifts 2px.
- **Viewer board** (`box-shadow: 0 0 0 1px var(--walnut-edge), 0 20px 36px -20px rgb(0 0 0 / 0.7)`).

### Named Rules
**The Tabletop Rule.** Shadows are soft, dark and downward, as if lit from above. No glow, no gloss, no coloured shadows.

## Shapes

Circles and slabs. Dials are perfect circles (SVG, 200-unit viewBox: black bezel, enamel face, 60 ticks with every fifth heavier). The case has a larger top radius than bottom (case-top over case-bottom), like a clock's rounded shoulders; it becomes a uniform 1.6rem on phones. Plungers are rounded on top and nearly square at the base (plunger-top over plunger-base), so they read as buttons emerging from the case. Toggles, tabs and the lookup bar are full pills; transport buttons are 8px squares; boards keep a 4px corner; list rows 6px.

## Components

### Plunger (primary action)
The brass button on top of the case. Source Sans 3 700, 0.95rem, sentence case, dial-black on brass; hover to brass-hi; pressing pushes it 5px down into the case and shortens its shadow. A plunger in the "down" position is a short brass stub. On the player page both plungers are decorative and swap up/down to show which side is running (importing).

### Enamel Dial
The signature readout. A square container holding the drawn face, with content inset 16% (22% when holding a board). Content is either a digit bank over a small grey caption, the username form, or a sample board ringed in dial black. A brass arc on the minute track shows progress; a dial-black second hand sweeps only while running.

### Digit Bank
Counts padded to fixed positions (three places by default, four for games imported); leading zeros stay at 18% opacity, and a zero count dims every digit. The accessible name is the formatted number.

### Buttons
- **Go button:** a 2.2rem brass circle with a dial-black arrow, the lookup's submit; hover brass-hi, 1px press.
- **Transport:** 2.6rem walnut-deep squares (8px) with line icons; hover walnut-rule; disabled at 35%.
- **Text link:** brass-hi, 600, 1px underline at 0.2em offset; hover to case cream.

### Toggles and Tabs
Pill tracks. On the dial face: enamel-shade track, selected pill dial-black with enamel text. In the top bar: selected pill walnut-rule. Ledger tabs ("By you / Against"): walnut-deep track, selected pill enamel with dial-black text.

### Inputs / Fields
- **On the dial:** the username is written on the face in Source Serif 4 (clamp(1.9rem, 3.4vw, 2.7rem)), centred, over a 2px dial-black underline that turns brass on focus. No box.
- **In the top bar:** a walnut-deep pill containing label, underlined field, platform toggle and go button.
- **Error:** one line of 600 text in flag (soft flag on walnut), announced as an alert.

### Ledger Row
A full-width ruled row: player's count in brass-hi on the left, motif mark in notation and name/definition in the middle, the opponents' count in warm linen on the right. Hover washes white at 3.5%. A motif with no finds sinks to the bottom, fades to faded linen, and reads "Not in these games yet." in italic; it is disabled rather than hidden.

### Specimen
A board thumbnail over its move in notation and a faded "vs opponent · date" line. Grid of auto-fill 11.5rem columns (two on phones). Hover lifts the board and rings it in brass.

Named-mate forms are specimen labels, not classifier tags. Only the ends of the scale are marked: a small "Textbook" (brass) or "Variant" (muted, with what differs beneath it). Characteristic, the normal form, carries no label. No pills, boxes or uppercase mono labels. The collapsed row names the mate; form totals appear only when it is opened.

### Engine Records
An optional layer after the deterministic patterns. Use ruled entries with small boards and evidence, two columns on desktop and one on phones; no rounded card backgrounds or enamel value chips. The opt-in action is a restrained outlined button. Visiting a fresh history does not start Stockfish, including underpromotion checks; the user must ask for analysis. Existing opted-in runs retain their resume behavior.

### Navigation
The top bar: wordmark left (a tiny enamel dial with a brass plunger, "ChessTrove" in Source Serif 4 600), player lookup right. On the landing the lookup is omitted because the clock is the form.

### Player Plate
Each side of the game viewer carries a plate: a 1.9rem clock face whose hand sweeps only for the side to move, the player's name, and the rating in a digit bank (enamel for the side to move, walnut-deep for the waiting side). Known gap: the plate does not show the per-move clock time, because clock data isn't stored yet; when it is, the time belongs in the plate as a digit bank.

### Move List
Source Code Pro throughout; rows 2.25rem high with a move-number column; the current move is an enamel cell with dial-black text; motif marks trail the move that produced them. Beside the board, a thin evaluation bar fills enamel from White's side over dial black.

## Do's and Don'ts

### Do:
- **Do** put every count in a digit bank with fixed positions and dimmed leading zeros.
- **Do** keep brass to controls and the player's own finds, and flag red to moments that went wrong.
- **Do** show a sweeping hand only while something is actually running (an import, the side to move).
- **Do** write empty motifs as "Not in these games yet": an invitation, disabled and faded, never removed.
- **Do** keep dials flat enamel with minute ticks; draw them, don't image them.
- **Do** set notation in Source Code Pro and nothing else in it.

### Don't:
- **Don't** add wood grain, brushed-brass or any other material texture; the walnut and brass are flat colour.
- **Don't** add gloss, glass highlights or glows to dials, plungers or the case.
- **Don't** introduce colours outside the palette, including a green "good" accent.
- **Don't** build the dark chess-app dashboard: search bar over a board, stat tiles, green accent.
- **Don't** set counts in the text face or in proportional figures.

# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

React + Vite frontend (user's choice), talking to the existing FastAPI backend (`/api/*`) over JSON.
Hosting must be free-tier (user constraint); the specific host is undecided.

## Users

Any online chess player (Chess.com or Lichess), arriving cold with nothing but their username. They
want to see what's hiding in their own game history: rare moments, their best finds, their worst misses.
No login: all source games are public, so the username is the key.

## Product Purpose

ChessTrove indexes a player's entire Chess.com/Lichess history so they can uncover rare motifs, unusual
positions, engine insights, and recurring patterns across every game they've played. Success: a player
types a username, their history imports and indexes on its own, and within minutes they're looking at
specific, verifiable moments from their own games (with links back to the original game).

## Positioning

Every other chess site reviews one game at a time. ChessTrove indexes all of a player's games at once
and searches across them: deterministic motif detection (smothered mates, underpromotions, back-rank
mates, missed mate-in-ones, and more) plus a persistent Stockfish index (only winning moves found,
missed wins, blunders) where every engine claim names the engine and settings that produced it.

## Operating Context

- Imports: Chess.com public API (monthly archives), Lichess export stream (rate-limited, ~20 games/s),
  PGN files. A 3,000-game history imports in minutes; Lichess is slower.
- Deterministic motifs are ready as soon as games import. Engine analysis is a separate, longer pass
  (planned to run in the visitor's browser); the product must be useful while it is partial.
- Players verify finds by opening the original game on Chess.com/Lichess.

## Capabilities and Constraints

- Motifs (deterministic, versioned): UNDERPROMOTION, PROMOTION_CHECKMATE, EN_PASSANT_CHECKMATE,
  KING_DELIVERED_MATE, DOUBLE_CHECK, THREE_PLUS_QUEENS, DOUBLE_DISAMBIGUATED_SAN, MISSED_MATE_IN_ONE,
  SMOTHERED_MATE, BACK_RANK_MATE. Plus 19 named mating patterns (Epaulette, Anastasia's, Boden's, Opera,
  Lolli's, Damiano's, ...): each match is a family member graded textbook, canonical or variant from explicit
  geometry, so broader usages stay searchable without passing for the classical diagram. Secondary in the UI,
  shown only once found.
- Engine labels (derived from stored evaluations): BLUNDER, MISSED_WIN, ONLY_WINNING_MOVE; underpromotion
  verdicts (best move among all legal moves; better than queening).
- Engine archaeology, the player page's "record book": career-wide discoveries, each opening at the exact
  position with its evidence.
  - Biggest comeback, and the inverse: best position in a lost game.
  - Biggest throw.
  - Only winning moves found, and only moves keeping a forced mate.
  - Underpromotions, judged: best move? vs. queening?
  - Sound queen, rook and exchange sacrifices.
  - Missed forced mates, and the longest forced mate carried out.
  - Unusual engine-approved moves.

  Derived at query time with every threshold a parameter. Engine facts and ChessTrove's definitions are
  kept apart, and nothing is called "brilliant".
- Usernames are unique per platform only; every player view is scoped to (platform, username).
- Undecided: host, browser-engine rollout, rate limiting for public imports.

## Brand Commitments

Name: ChessTrove. The user requires the design not look like generic AI output ("slop").

## Evidence on Hand

Real data from the user's own accounts (danksonpotato and dankiusdaddiuspotatoius on Chess.com,
dankdaddypotato on Lichess: 5,558 games) in the local database. No testimonials, user counts, or
press exist; none may be invented.

## Product Principles

1. Specific over general: show the actual move, board, and game, not a score.
2. Every claim is verifiable: link to the source game; engine claims name their settings.
3. Useful immediately: never block the page on the slow parts (imports, engine analysis).
4. Honest about rarity: a motif not found yet is an invitation, not a failure.

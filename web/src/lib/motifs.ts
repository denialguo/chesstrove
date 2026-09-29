// The vocabulary: every motif ChessTrove indexes, in a fixed order (rarest first) so players can
// compare pages. Glyphs follow chess annotation style; definitions match the backend detectors.

export interface MotifInfo { type: string; glyph: string; name: string; definition: string }

export const MOTIFS: MotifInfo[] = [
  { type: "EN_PASSANT_CHECKMATE", glyph: "e.p.#", name: "En passant mate", definition: "Checkmate by capturing en passant." },
  { type: "KING_DELIVERED_MATE", glyph: "K#", name: "The king mates", definition: "A king move delivers mate, by discovery or castling." },
  { type: "DOUBLE_DISAMBIGUATED_SAN", glyph: "Qh4e1", name: "Fully named move", definition: "Three identical pieces could reach the square, so the move names its file and rank." },
  { type: "SMOTHERED_MATE", glyph: "N#", name: "Smothered mate", definition: "A knight mates a king walled in by its own pieces." },
  { type: "PROMOTION_CHECKMATE", glyph: "=Q#", name: "Promotion mate", definition: "A pawn promotes with checkmate." },
  { type: "UNDERPROMOTION", glyph: "=N", name: "Underpromotion", definition: "A pawn promotes to a knight, bishop or rook instead of a queen." },
  { type: "THREE_PLUS_QUEENS", glyph: "3Q", name: "Three queens", definition: "Three or more queens on the board at once." },
  { type: "DOUBLE_CHECK", glyph: "++", name: "Double check", definition: "Two pieces give check at the same time." },
  { type: "BACK_RANK_MATE", glyph: "R#", name: "Back-rank mate", definition: "A rook or queen mates along a king's own back rank." },
  { type: "MISSED_MATE_IN_ONE", glyph: "#?", name: "Missed mate in one", definition: "Mate in one was on the board and a different move was played." },
];

export interface LabelInfo { type: string; glyph: string; name: string; definition: string; tone: "good" | "bad" }

export const ENGINE_LABELS: LabelInfo[] = [
  { type: "ONLY_WINNING_MOVE", glyph: "□", name: "Only winning moves found", definition: "Every other move would have let the win slip. Obvious recaptures don't count.", tone: "good" },
  { type: "MISSED_WIN", glyph: "?", name: "Missed wins", definition: "A clearly winning position (90%+ win chance) turned into one that wasn't (60% or less).", tone: "bad" },
  { type: "BLUNDER", glyph: "??", name: "Blunders", definition: "A move that cost at least 30 points of win chance.", tone: "bad" },
];

export const motifInfo = (type: string) => MOTIFS.find((m) => m.type === type);

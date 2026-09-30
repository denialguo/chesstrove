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

// Named mating patterns: secondary to the collection above, shown only once found. One mate can carry
// several names. Definitions match backend/detectors/named_mates.py.
const named = (type: string, name: string, definition: string): MotifInfo => ({ type, glyph: "#", name, definition });

export const NAMED_MATES: MotifInfo[] = [
  named("EPAULETTE_MATE", "Epaulette mate", "A queen mates head-on; the king's own pieces sit on both its shoulders. Classically two rooks."),
  named("SWALLOWS_TAIL_MATE", "Swallow's tail mate", "A guarded queen mates from right beside the king; its own pieces block the two squares behind it."),
  named("DOVETAIL_MATE", "Dovetail mate", "A queen mates from a diagonal touch; the king's own pieces fill the two squares she can't reach."),
  named("ANASTASIA_MATE", "Anastasia's mate", "A rook or queen mates along the edge; a knight covers the escapes and the king's own piece blocks the way in."),
  named("ARABIAN_MATE", "Arabian mate", "A rook mates from beside the king, guarded by a knight that also covers an escape. Classically in the corner."),
  named("BODEN_MATE", "Boden's mate", "Two bishops on crossing diagonals mate a king hemmed in by its own pieces."),
  named("OPERA_MATE", "Opera mate", "A rook mates on the edge beside the king, guarded by a bishop that also takes an escape square."),
  named("ANDERSSEN_MATE", "Anderssen's mate", "A rook or queen mates from the corner, guarded by a pawn that covers another escape."),
  named("LOLLI_MATE", "Lolli's mate", "A pawn-guarded queen mates from directly in front of the king (Qg7#)."),
  named("DAMIANO_MATE", "Damiano's mate", "A queen mates from the edge square diagonally in front of the king (Qh7# against Kg8), guarded by a pawn or a bishop."),
  named("MORPHY_MATE", "Morphy's mate", "A bishop mates a cornered king down the long diagonal while a rook seals the file."),
  named("GRECO_MATE", "Greco's mate", "A rook or queen mates a cornered king along the edge; a bishop covers the escape."),
  named("HOOK_MATE", "Hook mate", "A rook mates, guarded by a knight, guarded by a pawn."),
  named("CORRIDOR_MATE", "Corridor mate", "A back-rank mate on any other edge: the king's own pieces wall it in."),
  named("BLACKBURNE_MATE", "Blackburne's mate", "Two bishops and a knight do all the work."),
  named("RETI_MATE", "Réti's mate", "A bishop mates from beside a king walled in by its own pieces, guarded by a rook or queen down the line."),
  named("PILLSBURY_MATE", "Pillsbury's mate", "A rook mates coming straight in while a bishop covers the escape toward the corner."),
  named("LADDER_MATE", "Ladder mate", "Two heavy pieces: one mates along the edge, the other seals the next line in."),
  named("BOX_MATE", "Box mate", "The basic king-and-rook mate."),
];

export interface LabelInfo { type: string; glyph: string; name: string; definition: string; tone: "good" | "bad" }

export const ENGINE_LABELS: LabelInfo[] = [
  { type: "ONLY_WINNING_MOVE", glyph: "□", name: "Only winning moves found", definition: "Every other move would have let the win slip. Obvious recaptures don't count.", tone: "good" },
  { type: "MISSED_WIN", glyph: "?", name: "Missed wins", definition: "A clearly winning position (90%+ win chance) turned into one that wasn't (60% or less).", tone: "bad" },
  { type: "BLUNDER", glyph: "??", name: "Blunders", definition: "A move that cost at least 30 points of win chance.", tone: "bad" },
];

// Named mates come in three forms (backend: named_mates.grade). `short_of` names the traits that kept one
// out of the next form up; these say what differs from the classical picture, not what's wrong.
export type MateForm = "textbook" | "canonical" | "variant";
export const FORM_NAME: Record<MateForm, string> = { textbook: "Textbook", canonical: "Canonical", variant: "Variant" };
const DIFFERS: Record<string, string> = {
  no_extra_helpers: "other pieces help close the net",
  both_shoulders_are_rooks: "the shoulder pieces aren't both rooks",
  king_on_edge: "the king isn't backed against the edge",
  queen_two_squares_away: "the queen checks from further away",
  knight_controls_both_characteristic_flights: "the knight covers only one of the two escape squares",
  king_on_side_file: "it happens on a back rank rather than a side file",
  king_next_to_corner: "the king isn't next to the corner",
  king_in_corner: "the king isn't in the corner",
  king_on_home_rank: "the king isn't on its own back rank",
  king_near_corner: "the king isn't near a corner",
  support_is_pawn: "a bishop supports the queen instead of a pawn",
  checker_is_rook: "a queen mates instead of a rook",
  checked_down_side_file: "the mate comes along the back rank",
  four_own_blockers: "fewer of the king's own pieces wall it in",
  supporter_is_rook: "a queen guards the bishop instead of a rook",
  bishop_covers_corner: "the bishop covers the square toward the corner, not the corner itself",
  rook_from_distance: "the rook mates from right beside the king",
  only_tail_pieces_block: "more of the king's own pieces hem it in than the two tail pieces",
  own_blockers_close_squares: "none of the king's own pieces hem it in",
};
/** "Variant: the shoulder pieces aren't both rooks." for a named-mate event; null for other events. */
export function formNote(metadata: Record<string, unknown>): { form: MateForm; note: string } | null {
  const form = metadata.form as MateForm | undefined;
  if (!form) return null;
  const why = ((metadata.short_of as string[] | undefined) ?? []).map((t) => DIFFERS[t] ?? t.replaceAll("_", " "));
  return { form, note: why.length ? `${why.join("; ")}.`.replace(/^./, (c) => c.toUpperCase()) : "" };
}

export const motifInfo = (type: string) => [...MOTIFS, ...NAMED_MATES].find((m) => m.type === type);

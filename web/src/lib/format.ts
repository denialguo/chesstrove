const dateFmt = new Intl.DateTimeFormat("en", { day: "numeric", month: "short", year: "numeric" });
const monthFmt = new Intl.DateTimeFormat("en", { month: "long", year: "numeric" });
const num = new Intl.NumberFormat("en");

export const formatDate = (iso: string | null) => (iso ? dateFmt.format(new Date(iso)) : "Undated");
export const formatMonth = (iso: string | null) => (iso ? monthFmt.format(new Date(iso)) : "");
export const n = (value: number) => num.format(value);
export const plural = (count: number, one: string, many = `${one}s`) => `${n(count)} ${count === 1 ? one : many}`;

/** "29…Nf2#" / "30.Qe6": move number notation from a ply (1-based). */
/** A deliberately rough time: estimates from a rate that keeps changing shouldn't look precise. */
export function roughDuration(seconds: number) {
  const m = seconds / 60;
  return m < 2 ? "a minute or two" : m < 60 ? `about ${m < 10 ? Math.round(m) : Math.round(m / 5) * 5} minutes`
    : `about ${Math.round(m / 30) / 2} hours`;
}

export function moveLabel(ply: number, san: string, color: "w" | "b") {
  const number = Math.ceil(ply / 2);
  return color === "w" ? `${number}.${san}` : `${number}…${san}`;
}


/** "3+2" / "10 min" / "1 day" from a PGN TimeControl. */
export function timeControl(tc: string | null) {
  if (!tc) return null;
  if (tc.includes("/")) {
    const days = Math.round(Number(tc.split("/")[1]) / 86400);
    return `${days} ${days === 1 ? "day" : "days"} per move`;
  }
  const [base, inc] = tc.split("+").map(Number);
  const minutes = base / 60;
  const baseLabel = Number.isInteger(minutes) ? `${minutes}` : `${Math.round(base)}s`;
  return inc ? `${baseLabel}+${inc}` : `${baseLabel} min`;
}

/** A player-side evaluation, short: "+1.8", "−0.4", "M3", "−M2", "#". */
export function evalShort(e: { cp: number } | { mate: number } | null | undefined) {
  if (!e) return "–";
  if ("mate" in e) return e.mate === 0 ? "#" : e.mate > 0 ? `M${e.mate}` : `−M${-e.mate}`;
  const v = e.cp / 100;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}`;
}

/** The same in words: "mate in 3", "mated in 2", "+1.8". */
export function evalWords(e: { cp: number } | { mate: number } | null | undefined) {
  if (!e) return "unknown";
  if ("mate" in e) return e.mate === 0 ? "mate on the board" : e.mate > 0 ? `mate in ${e.mate}` : `mated in ${-e.mate}`;
  return evalShort(e);
}

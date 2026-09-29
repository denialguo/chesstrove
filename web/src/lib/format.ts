const dateFmt = new Intl.DateTimeFormat("en", { day: "numeric", month: "short", year: "numeric" });
const monthFmt = new Intl.DateTimeFormat("en", { month: "long", year: "numeric" });
const num = new Intl.NumberFormat("en");

export const formatDate = (iso: string | null) => (iso ? dateFmt.format(new Date(iso)) : "Undated");
export const formatMonth = (iso: string | null) => (iso ? monthFmt.format(new Date(iso)) : "");
export const n = (value: number) => num.format(value);
export const plural = (count: number, one: string, many = `${one}s`) => `${n(count)} ${count === 1 ? one : many}`;

/** "29…Nf2#" / "30.Qe6": move number notation from a ply (1-based). */
export function moveLabel(ply: number, san: string, color: "w" | "b") {
  const number = Math.ceil(ply / 2);
  return color === "w" ? `${number}.${san}` : `${number}…${san}`;
}

/** Win chance as a whole percentage. */
export const pct = (expected: number) => `${Math.round(expected * 100)}%`;

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

/** An engine score from the mover's side: "+1.8", "−0.4", "M3" (mover mates), "−M2" (mover gets mated), "#". */
export function evalText(cp: number | null, mate: number | null, color: "w" | "b") {
  const sign = color === "w" ? 1 : -1;
  if (mate !== null) {
    if (mate === 0) return "#";
    const pov = mate * sign;
    return pov > 0 ? `M${pov}` : `−M${-pov}`;
  }
  const pov = ((cp ?? 0) * sign) / 100;
  return `${pov > 0 ? "+" : pov < 0 ? "−" : ""}${Math.abs(pov).toFixed(1)}`;
}

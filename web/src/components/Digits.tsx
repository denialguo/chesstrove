import { n } from "../lib/format";

/** A count in fixed digit positions, like a dial's numerals: leading zeros stay, dimmed. */
export function Digits({ value, places = 3, className = "" }: { value: number; places?: number; className?: string }) {
  const text = String(value);
  const padded = text.padStart(Math.max(places, text.length), "0");
  const lead = padded.length - text.length;
  return (
    <span className={`digits ${value === 0 ? "digits--zero" : ""} ${className}`} aria-label={n(value)} role="img">
      {padded.split("").map((d, i) => (
        <span key={i} className={i < lead ? "digits__lead" : undefined} aria-hidden="true">{d}</span>
      ))}
    </span>
  );
}

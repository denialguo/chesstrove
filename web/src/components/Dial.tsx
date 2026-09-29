import type { CSSProperties, ReactNode } from "react";

interface DialProps {
  children: ReactNode;
  /** 0..1 fills the minute track in brass (import / analysis progress). */
  progress?: number;
  /** A sweeping second hand: the clock is running. */
  running?: boolean;
  className?: string;
  style?: CSSProperties;
}

const TICKS = Array.from({ length: 60 }, (_, i) => i);

/** A white enamel chess-clock dial. Content sits on the face; the ticks, bezel and hand are drawn. */
export function Dial({ children, progress, running, className = "", style }: DialProps) {
  const r = 84;
  const circumference = 2 * Math.PI * r;
  return (
    <div className={`dial ${running ? "dial--running" : ""} ${className}`} style={style}>
      <svg className="dial__face" viewBox="0 0 200 200" aria-hidden="true">
        <circle cx="100" cy="100" r="99" className="dial__bezel" />
        <circle cx="100" cy="100" r="94" className="dial__enamel" />
        {TICKS.map((i) => {
          const hour = i % 5 === 0;
          return (
            <line key={i} x1="100" y1={hour ? 9.5 : 10.5} x2="100" y2={hour ? 19 : 15}
              className={hour ? "dial__tick dial__tick--hour" : "dial__tick"}
              transform={`rotate(${i * 6} 100 100)`} />
          );
        })}
        {progress !== undefined && (
          <circle cx="100" cy="100" r={r} className="dial__progress"
            strokeDasharray={`${Math.max(0, Math.min(1, progress)) * circumference} ${circumference}`}
            transform="rotate(-90 100 100)" />
        )}
        {running && (
          <g className="dial__hand">
            <line x1="100" y1="112" x2="100" y2="22" />
            <circle cx="100" cy="100" r="3.2" />
          </g>
        )}
      </svg>
      <div className="dial__content">{children}</div>
    </div>
  );
}

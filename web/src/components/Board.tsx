import { useEffect, useRef } from "react";
import { Chessground } from "chessground";
import type { Api } from "chessground/api";
import type { DrawBrushes, DrawShape } from "chessground/draw";
import type { Key } from "chessground/types";

interface BoardProps {
  fen: string;
  orientation?: "white" | "black";
  lastMove?: string | null; // UCI
  arrows?: { uci: string; brush: "brass" | "flag" }[];
  check?: boolean;
  className?: string;
  label?: string;
}

const squares = (uci: string) => [uci.slice(0, 2), uci.slice(2, 4)] as Key[];

/** A read-only chessground board. */
export function Board({ fen, orientation = "white", lastMove, arrows = [], check, className = "", label }: BoardProps) {
  const host = useRef<HTMLDivElement>(null);
  const cg = useRef<Api | null>(null);
  const shapes: DrawShape[] = arrows.map((a) => {
    const [orig, dest] = squares(a.uci);
    return { orig, dest, brush: a.brush };
  });
  const config = {
    fen,
    orientation,
    lastMove: lastMove ? squares(lastMove) : undefined,
    check: check || undefined,
    drawable: { autoShapes: shapes },
  };

  useEffect(() => {
    cg.current = Chessground(host.current!, {
      ...config,
      viewOnly: true,
      coordinates: false,
      animation: { enabled: true, duration: 180 },
      drawable: {
        enabled: false,
        visible: true,
        autoShapes: shapes,
        // chessground deep-merges these into its default brushes; the type wants the full set
        brushes: {
          brass: { key: "brass", color: "#B28A3E", opacity: 0.9, lineWidth: 11 },
          flag: { key: "flag", color: "#C8222C", opacity: 0.85, lineWidth: 11 },
        } as unknown as DrawBrushes,
      },
    });
    return () => cg.current?.destroy();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    cg.current?.set(config);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fen, orientation, lastMove, check, JSON.stringify(arrows)]);

  return (
    <div className={`board ${className}`} role="img" aria-label={label ?? `Chess position ${fen}`}>
      <div ref={host} className="board__cg" />
    </div>
  );
}

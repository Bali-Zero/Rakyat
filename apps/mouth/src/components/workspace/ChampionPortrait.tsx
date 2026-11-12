"use client";

import { useState, type CSSProperties } from "react";
import Image from "next/image";
import { initialsOf } from "@/lib/team-initials";
import { cn } from "@/lib/utils";

/**
 * Where the face sits on each approved staff photo: [x %, y %, zoom] of the
 * source bitmap. The staff cards print the name, role and logo beside the
 * face, so a `face` portrait zooms the circle onto the face instead of the
 * whole card. Display framing only — the bitmaps stay untouched, and a file
 * missing here keeps the plain centred crop.
 */
const FACE_FRAMING: Record<string, readonly [number, number, number]> = {
  "adit.jpg": [50, 34, 1.35],
  "angel.jpg": [50, 34, 1.35],
  "ari.jpg": [50, 34, 1.35],
  "asya.jpg": [50, 34, 1.35],
  "candra.jpg": [50, 34, 1.35],
  "damar.jpg": [50, 34, 1.35],
  "dea.jpg": [50, 34, 1.35],
  "dewaayu.jpg": [50, 34, 1.35],
  "heru-komisaris.jpg": [50, 34, 1.35],
  "kadek.jpg": [50, 34, 1.35],
  "krisna-20260927.jpg": [50, 34, 1.35],
  "krisna.jpg": [50, 34, 1.35],
  "rina.jpg": [50, 34, 1.35],
  "rio.jpg": [50, 34, 1.35],
  "ruslana-2026-11.jpg": [50, 34, 1.35],
  "ruslana.jpg": [50, 34, 1.35],
  "subhi.jpg": [50, 34, 1.35],
  "surya.jpg": [50, 34, 1.35],
  "vino.jpg": [50, 34, 1.35],
  "zainal-ceo.jpg": [50, 34, 1.35],
  "zero.jpg": [50, 34, 1.35],
};

/**
 * The leaderboard's avatar_url comes from team_members.avatar, which still
 * names a superseded photo; the approved one ships under a versioned name.
 * Exact local paths only — anything else goes through the checks unchanged.
 */
const SUPERSEDED_SOURCES = new Map<string, string>([
  ["/static/team/krisna.jpg", "/static/team/krisna-20260927.jpg"],
  ["/static/team/ruslana.jpg", "/static/team/ruslana-2026-11.jpg"],
]);

/**
 * object-position pins the focal point of the covered image at the same
 * percentage of the box; the transform then moves that point to the centre
 * and zooms around it.
 */
function faceFraming(src: string): CSSProperties | undefined {
  const framing = FACE_FRAMING[src.slice(src.lastIndexOf("/") + 1)];
  if (!framing) return undefined;
  const [x, y, zoom] = framing;
  return {
    objectPosition: `${x}% ${y}%`,
    transformOrigin: "0 0",
    transform: `translate(50%, 50%) scale(${zoom}) translate(-${x}%, -${y}%)`,
  };
}

export function ChampionPortrait({
  name,
  src,
  className,
  face = false,
  sizes = "(max-width: 640px) 120px, 220px",
}: {
  name: string;
  src?: string | null;
  className?: string;
  face?: boolean;
  sizes?: string;
}) {
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const source = src ? (SUPERSEDED_SOURCES.get(src) ?? src) : src;
  const safeSource =
    source && /^\/static\/team\/[a-z0-9_-]+\.(jpg|jpeg|png|webp)$/i.test(source)
      ? source
      : null;
  return (
    <span
      className={cn(
        "relative isolate inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-[var(--bz-card-hover)] text-[var(--bz-copper-text)]",
        className,
      )}
    >
      {safeSource && safeSource !== failedSource ? (
        <Image
          src={safeSource}
          alt={name}
          fill
          sizes={sizes}
          className="object-cover"
          style={face ? faceFraming(safeSource) : undefined}
          onError={() => setFailedSource(safeSource)}
        />
      ) : (
        <span role="img" aria-label={name} className="text-2xl font-bold">
          {initialsOf(name)}
        </span>
      )}
    </span>
  );
}

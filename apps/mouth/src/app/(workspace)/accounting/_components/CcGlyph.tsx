export type CcGlyphName =
  | "confirm"
  | "link"
  | "choose"
  | "nomatch"
  | "upload"
  | "arrow"
  | "lock"
  | "tick"
  | "undo"
  | "out";

export const CC_PRIMARY =
  "inline-flex h-11 shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-[10px] bg-[var(--cc-act)] px-[22px] text-sm font-bold text-[var(--cc-act-on)] disabled:opacity-50";
export const CC_SECONDARY =
  "inline-flex h-10 shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-[10px] border border-[var(--cc-line-control)] bg-[var(--cc-card)] px-4 text-sm font-bold text-[var(--cc-tx)]";
export const CC_TEXT =
  "inline-flex h-10 shrink-0 items-center whitespace-nowrap px-2 text-sm font-bold text-[var(--cc-tx)] underline decoration-1 underline-offset-4";

export function CcGlyph({
  name,
  className,
}: {
  name: CcGlyphName;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      data-glyph={name}
      className={className}
    >
      {name === "confirm" ? (
        <>
          <circle
            cx="12"
            cy="12"
            r="9.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          />
          <path
            d="M7.6 12.4l3 3 5.8-6.4"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </>
      ) : name === "link" ? (
        <path
          d="M10 7H8a5 5 0 0 0 0 10h2M14 7h2a5 5 0 0 1 0 10h-2M8.5 12h7"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
        />
      ) : name === "choose" ? (
        <>
          <path
            d="M12 2.6 21.4 12 12 21.4 2.6 12Z"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinejoin="round"
          />
          <path
            d="M9.8 9.6a2.3 2.3 0 1 1 3.1 2.15c-.6.25-.9.7-.9 1.3v.35"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
          <circle cx="12" cy="16" r="1.15" fill="currentColor" />
        </>
      ) : name === "nomatch" ? (
        <>
          <circle
            cx="12"
            cy="12"
            r="9.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          />
          <path
            d="M5.5 18.5 18.5 5.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </>
      ) : name === "upload" ? (
        <path
          d="M12 15V4M7 9l5-5 5 5M4 15v3.5A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V15"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ) : name === "arrow" ? (
        <path
          d="M5 12h14M13 6l6 6-6 6"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ) : name === "lock" ? (
        <>
          <rect
            x="4.5"
            y="10.5"
            width="15"
            height="10.5"
            rx="2.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          />
          <path
            d="M8 10.5V8a4 4 0 0 1 8 0v2.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          />
        </>
      ) : name === "tick" ? (
        <path
          d="M5 12.5l4.5 4.5L19 7.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ) : name === "undo" ? (
        <path
          d="M4 9h11a5 5 0 0 1 0 10H9M4 9l4-4M4 9l4 4"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ) : (
        <path
          d="M7 17 17 7M8.5 7H17v8.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  );
}

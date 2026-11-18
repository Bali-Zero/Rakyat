export function CalmArt() {
  return (
    <span className="block h-[190px] w-[190px] overflow-hidden rounded-full">
      <svg
        width="190"
        height="190"
        viewBox="0 0 190 190"
        aria-hidden="true"
        data-art="calm"
      >
        <rect width="190" height="190" className="fill-[var(--cc-art-sky)]" />
        <circle cx="95" cy="88" r="44" className="fill-[var(--cc-art-sun)]" />
        <path
          d="M74 88l14 14 27-30"
          fill="none"
          className="stroke-[color:var(--cc-art-hill3)]"
          strokeWidth="8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d="M0 128C40 108 70 110 100 124s70 6 90-4V190H0Z"
          className="fill-[var(--cc-art-hill1)]"
        />
        <path
          d="M0 150c36-14 70-14 104 0s62 10 86 2V190H0Z"
          className="fill-[var(--cc-confirm)]"
        />
        <path
          d="M0 172c50-8 110-8 190 2V190H0Z"
          className="fill-[var(--cc-art-hill3)]"
        />
      </svg>
    </span>
  );
}

export function DayOneArt() {
  return (
    <svg
      width="220"
      height="220"
      viewBox="0 0 220 220"
      aria-hidden="true"
      data-art="dayone"
    >
      <circle cx="110" cy="110" r="96" className="fill-[var(--cc-art-sun)]" />
      <rect
        x="58"
        y="44"
        width="104"
        height="134"
        rx="12"
        className="fill-[var(--cc-art-sheet)] stroke-[color:var(--cc-art-sheet-line)]"
        strokeWidth="2"
      />
      <rect
        x="74"
        y="64"
        width="46"
        height="8"
        rx="4"
        className="fill-[var(--cc-art-sheet-head)]"
      />
      <rect
        x="74"
        y="86"
        width="72"
        height="6"
        rx="3"
        className="fill-[var(--cc-art-sheet-line)]"
      />
      <rect
        x="74"
        y="102"
        width="60"
        height="6"
        rx="3"
        className="fill-[var(--cc-art-sheet-line)]"
      />
      <rect
        x="74"
        y="118"
        width="72"
        height="6"
        rx="3"
        className="fill-[var(--cc-art-sheet-line)]"
      />
      <rect
        x="74"
        y="134"
        width="40"
        height="6"
        rx="3"
        className="fill-[var(--cc-art-sheet-line)]"
      />
      <circle cx="156" cy="158" r="30" className="fill-[var(--cc-act)]" />
      <path
        d="M156 172v-27M145 156l11-11 11 11"
        fill="none"
        className="stroke-[color:var(--cc-act-on)]"
        strokeWidth="5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

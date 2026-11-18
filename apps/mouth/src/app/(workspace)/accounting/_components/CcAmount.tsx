export function CcAmount({ value }: { value: number }) {
  const digits = new Intl.NumberFormat("id-ID", {
    maximumFractionDigits: 0,
  }).format(Math.abs(value));
  return (
    <span className="font-serif tabular-nums">
      {value < 0 ? "-" : ""}
      <span className="mr-[0.28em] text-[0.72em] font-medium text-[var(--cc-tx-2)]">
        Rp
      </span>
      {digits}
    </span>
  );
}

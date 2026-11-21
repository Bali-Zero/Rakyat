// Identity masking for the client portal (moved out of the profile page,
// PR #180, so the Family page masks with the same rules).
//
// Passport numbers and dates of birth used to render in full, readable by
// anyone looking at the screen. Pages show these masked forms instead.

export const MASK = "•••••";

/** Keeps the last three characters so the client can recognise the document. */
export const maskPassportNumber = (value: string): string =>
  value.length > 3 ? `${MASK}${value.slice(-3)}` : MASK;

/** Keeps only the birth year (expects an ISO `YYYY-…` value). */
export const maskDateOfBirth = (value: string): string => {
  const year = /^(\d{4})/.exec(value)?.[1];
  return year ? `•• ••• ${year}` : "•• ••• ••••";
};

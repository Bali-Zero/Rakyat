const BOARD_MEMBER_ROLE = "board member";

export function canAssignPracticeToAnyone(
  isAdmin: boolean,
  role: string | null | undefined,
): boolean {
  if (isAdmin) return true;
  return (role ?? "").trim().toLowerCase() === BOARD_MEMBER_ROLE;
}

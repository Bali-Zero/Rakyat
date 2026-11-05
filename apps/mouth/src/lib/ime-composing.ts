/**
 * True while an input-method editor (Chinese, Japanese, Korean, …) is still
 * composing. The Enter that commits a candidate fires `keydown` too; a submit
 * handler that acts on it sends the message half-typed. Safari reports
 * `isComposing: false` on that keydown and only exposes `keyCode === 229`,
 * so both signals are read. Accepts a React synthetic event or a native one.
 */
export type ComposingKeyEvent = {
  keyCode?: number;
  isComposing?: boolean;
  nativeEvent?: { isComposing?: boolean };
};

export function isImeComposing(event: ComposingKeyEvent): boolean {
  if (event.nativeEvent?.isComposing) return true;
  if (event.isComposing) return true;
  return event.keyCode === 229;
}

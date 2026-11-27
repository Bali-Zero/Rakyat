/**
 * Longest visitor message (after trim) the voice concierge accepts. The route
 * refuses a longer one with `400 message too long`; the page refuses it before
 * sending. One constant, so the two cannot drift apart.
 */
export const VOICE_CONCIERGE_MAX_MESSAGE_LENGTH = 1200;

/**
 * Parse user-entered coordinate strings into {lat, lng} decimal degrees.
 *
 * Accepted formats:
 *  - Decimal: "-8.65, 115.13" or "-8.65 115.13"
 *  - DMS (Google Maps paste): 8°39'17.4"S 115°08'22.3"E
 *    (quote variants: ° ' " or ° ′ ″ or just spaces)
 *  - Google Maps URL: https://maps.google.com/?q=-8.65,115.13 (query ?q=lat,lng)
 *
 * Returns null if the input cannot be parsed with certainty: text around a
 * decimal pair, a comma with digits on both sides that may be a decimal comma
 * ("-8,65, 115,13") and a latitude outside [-90, 90] or a longitude outside
 * [-180, 180].
 *
 * Notes on DMS:
 *  - Deg + min/60 + sec/3600
 *  - Negative for S (south) or W (west)
 *  - Positive for N (north) or E (east)
 */

export interface LatLng {
  lat: number;
  lng: number;
}

const DMS_PART =
  /(\d+(?:\.\d+)?)\s*°\s*(?:(\d+(?:\.\d+)?)\s*['′\u2032]\s*)?(?:(\d+(?:\.\d+)?)\s*["″\u2033]\s*)?([NSEW])/i;

function dmsToDecimal(match: RegExpMatchArray): number {
  const deg = Number(match[1]);
  const min = match[2] ? Number(match[2]) : 0;
  const sec = match[3] ? Number(match[3]) : 0;
  const hemi = match[4].toUpperCase();
  let decimal = deg + min / 60 + sec / 3600;
  if (hemi === "S" || hemi === "W") decimal = -decimal;
  return decimal;
}

const DECIMAL_PAIR = /^(-?\d+(?:\.\d+)?)(?:\s*,\s*|\s+)(-?\d+(?:\.\d+)?)$/;
const DOTTED_PAIR = /^-?\d+\.\d+,-?\d+\.\d+$/;

function inRange(point: LatLng): LatLng | null {
  const { lat, lng } = point;
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
  return point;
}

export function parseCoordinates(input: string): LatLng | null {
  const raw = input.trim();
  if (!raw) return null;

  // Try Google Maps URL: ?q=LAT,LNG or /@LAT,LNG,ZOOM
  const urlMatch =
    raw.match(/[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)/) ??
    raw.match(/\/@(-?\d+\.\d+),(-?\d+\.\d+)/);
  if (urlMatch) {
    const lat = Number(urlMatch[1]);
    const lng = Number(urlMatch[2]);
    return inRange({ lat, lng });
  }

  // Try DMS: two matches (lat + lng with N/S/E/W markers)
  const dmsRegex = new RegExp(DMS_PART.source, "gi");
  const dmsMatches = Array.from(raw.matchAll(dmsRegex));
  if (dmsMatches.length === 2) {
    const first = dmsToDecimal(dmsMatches[0]);
    const second = dmsToDecimal(dmsMatches[1]);
    const firstHemi = dmsMatches[0][4].toUpperCase();
    const secondHemi = dmsMatches[1][4].toUpperCase();
    // Figure out which is lat (N/S) and which is lng (E/W)
    const firstIsLat = firstHemi === "N" || firstHemi === "S";
    const secondIsLat = secondHemi === "N" || secondHemi === "S";
    if (firstIsLat && !secondIsLat) {
      return inRange({ lat: first, lng: second });
    }
    if (!firstIsLat && secondIsLat) {
      return inRange({ lat: second, lng: first });
    }
    return null;
  }

  // Fallback: the whole input is one decimal pair. A digit,comma,digit with
  // no space may be a decimal comma, so it is not guessed, unless both numbers
  // carry a dot (then the comma can only be the separator).
  if (/\d,\d/.test(raw) && !DOTTED_PAIR.test(raw)) return null;
  const decimalMatch = raw.match(DECIMAL_PAIR);
  if (decimalMatch) {
    return inRange({
      lat: Number(decimalMatch[1]),
      lng: Number(decimalMatch[2]),
    });
  }

  return null;
}

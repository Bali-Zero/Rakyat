import { describe, expect, it } from "vitest";
import { parseCoordinates } from "./parse-coordinates";

describe("parseCoordinates safety", () => {
  it("returns null for decimal commas instead of guessing", () => {
    expect(parseCoordinates("-8,65, 115,13")).toBeNull();
    expect(parseCoordinates("-8,65 115,13")).toBeNull();
  });

  it("returns null for a latitude or longitude out of range", () => {
    expect(parseCoordinates("-8.65, 215.13")).toBeNull();
    expect(parseCoordinates("115.13, -8.65")).toBeNull();
    expect(parseCoordinates("91, 10")).toBeNull();
    expect(
      parseCoordinates("https://maps.google.com/?q=-98.65,115.13"),
    ).toBeNull();
    expect(parseCoordinates("95°39'S 115°08'E")).toBeNull();
  });

  it("returns null when text surrounds the pair", () => {
    expect(parseCoordinates("see -8.65, 115.13 please")).toBeNull();
    expect(parseCoordinates("Plot 12 34 extra 56")).toBeNull();
  });

  it("returns null for more than two numbers", () => {
    expect(parseCoordinates("-8.65, 115.13, 17")).toBeNull();
  });

  it("still parses unambiguous decimal pairs", () => {
    expect(parseCoordinates("-8.65, 115.13")).toEqual({
      lat: -8.65,
      lng: 115.13,
    });
    expect(parseCoordinates("-8.65,115.13")).toEqual({
      lat: -8.65,
      lng: 115.13,
    });
    expect(parseCoordinates("-8.65 115.13")).toEqual({
      lat: -8.65,
      lng: 115.13,
    });
    expect(parseCoordinates("90, 180")).toEqual({ lat: 90, lng: 180 });
  });

  it("still parses the map link and DMS forms", () => {
    expect(
      parseCoordinates("https://www.google.com/maps/@-8.648,115.132,17z"),
    ).toEqual({ lat: -8.648, lng: 115.132 });
    expect(parseCoordinates("8°39'S 115°08'E")?.lat).toBeCloseTo(-8.65, 2);
  });
});

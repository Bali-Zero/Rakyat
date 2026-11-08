import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC = path.join(process.cwd(), "src");

function* walk(dir: string): Generator<string> {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) yield* walk(p);
    else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\.tsx?$/.test(e.name))
      yield p;
  }
}

// Every dangerouslySetInnerHTML prop expression (balanced braces after the "=") that holds a
// raw JSON.stringify( is a JSON-LD sink that can be closed from inside by "</script>".
describe("no dangerouslySetInnerHTML carries a raw JSON.stringify", () => {
  it("scans the mouth source", () => {
    const offenders: string[] = [];
    let sinks = 0;
    for (const file of walk(SRC)) {
      const text = fs.readFileSync(file, "utf8");
      let i = text.indexOf("dangerouslySetInnerHTML=");
      while (i !== -1) {
        sinks++;
        const open = text.indexOf("{", i);
        let depth = 0;
        let j = open;
        for (; j < text.length; j++) {
          if (text[j] === "{") depth++;
          else if (text[j] === "}" && --depth === 0) break;
        }
        const expr = text.slice(open, j + 1);
        if (expr.includes("JSON.stringify(")) {
          offenders.push(
            `${path.relative(SRC, file)}:${text.slice(0, i).split("\n").length}`,
          );
        }
        i = text.indexOf("dangerouslySetInnerHTML=", j);
      }
    }
    expect(sinks).toBeGreaterThan(20);
    expect(offenders).toEqual([]);
  });
});

import ts from "typescript";

export const PRICE_LITERAL_RE =
  /(?:\b(?:Rp\.?|IDR)\s*[0-9]|(?<![0-9][.,]?)[0-9]+(?:[.,][0-9]+)*(?![.,]?[0-9])\s*(?:(?:(?:juta|jt|miliar|M|B|K|rb|ribu|million|billion|thousand)\b\.?\s*)?(?:IDR|rupiah)\b|(?:juta|jt)\b))/i;

export const HISTORICAL_PRICE_RE =
  /35[,.]?000[,.]?000|39[,.]?000[,.]?000|\b39M\b|\b35M\b/i;

function normalized(text: string): string | undefined {
  const result = text.replace(/\s+/g, " ").trim();
  return result || undefined;
}

function literalExpression(
  expression: ts.Expression,
): ts.StringLiteral | ts.NoSubstitutionTemplateLiteral | undefined {
  let current = expression;
  while (ts.isParenthesizedExpression(current)) current = current.expression;
  return ts.isStringLiteral(current) ||
    ts.isNoSubstitutionTemplateLiteral(current)
    ? current
    : undefined;
}

/** Text that can be displayed by a TypeScript/TSX source file. */
export function visibleSourceText(
  source: string,
  fileName = "fixture.tsx",
): string[] {
  const kind =
    fileName.endsWith(".ts") && !fileName.endsWith(".tsx")
      ? ts.ScriptKind.TS
      : ts.ScriptKind.TSX;
  const file = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    kind,
  );
  const out: string[] = [];
  const add = (text: string) => {
    const result = normalized(text);
    if (result) out.push(result);
  };

  const visitJsx = (node: ts.JsxElement | ts.JsxFragment) => {
    let segment = "";
    const flush = () => {
      add(segment);
      segment = "";
    };
    const children = node.children;
    for (const child of children) {
      if (ts.isJsxText(child)) {
        segment += child.text;
      } else if (ts.isJsxExpression(child)) {
        if (child.expression) {
          const literal = literalExpression(child.expression);
          if (literal) segment += literal.text;
          else {
            flush();
            visit(child.expression);
          }
        }
      } else {
        flush();
        visit(child);
      }
    }
    flush();
    if (ts.isJsxElement(node)) {
      visit(node.openingElement);
      visit(node.closingElement);
    } else {
      visit(node.openingFragment);
      visit(node.closingFragment);
    }
  };

  const visit = (node: ts.Node): void => {
    if (ts.isJsxElement(node) || ts.isJsxFragment(node)) {
      visitJsx(node);
    } else if (ts.isJsxText(node)) {
      return;
    } else if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node)
    ) {
      add(node.text);
    } else if (ts.isTemplateExpression(node)) {
      add(node.head.text);
      for (const span of node.templateSpans) {
        visit(span.expression);
        add(span.literal.text);
      }
    } else {
      ts.forEachChild(node, visit);
    }
  };

  visit(file);
  return out;
}

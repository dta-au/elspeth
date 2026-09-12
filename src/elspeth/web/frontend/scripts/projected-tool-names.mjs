import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";

/** Read only literal projector ownership; unsupported declarations fail closed. */
export function projectedToolNames(source) {
  const file = ts.createSourceFile("ProposalDiff.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  if (file.parseDiagnostics.length > 0) throw new Error("Invalid projector TypeScript");
  const declarations = file.statements
    .filter(ts.isVariableStatement)
    .flatMap((statement) => [...statement.declarationList.declarations])
    .filter((declaration) => ts.isIdentifier(declaration.name) && declaration.name.text === "TOOL_PROJECTORS");
  if (declarations.length !== 1) throw new Error("Expected one TOOL_PROJECTORS declaration");
  let initializer = declarations[0].initializer;
  if (initializer && ts.isSatisfiesExpression(initializer)) initializer = initializer.expression;
  if (!initializer || !ts.isObjectLiteralExpression(initializer)) {
    throw new Error("TOOL_PROJECTORS must own a literal object");
  }
  const names = initializer.properties.map((property) => {
    if (!ts.isPropertyAssignment(property)
      || (!ts.isIdentifier(property.name) && !ts.isStringLiteral(property.name))
      || !ts.isArrowFunction(property.initializer)) {
      throw new Error("Projectors must be literal named arrow functions; no spreads or computed keys");
    }
    return property.name.text;
  });
  if (names.length === 0 || new Set(names).size !== names.length || names.includes("__proto__")) {
    throw new Error("Projector keys must be nonempty, unique own properties");
  }
  return names;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const source = readFileSync(new URL("../src/components/chat/ProposalDiff.tsx", import.meta.url), "utf8");
  process.stdout.write(JSON.stringify(projectedToolNames(source)) + "\n");
}

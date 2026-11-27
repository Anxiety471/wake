/**
 * Read the comments of a JS or TS file with the parser eslint itself uses.
 *
 * `tests/test_inline_suppressions.py` reads eslint directives out of every tracked JS/TS file. A
 * directive is only a directive where eslint sees a comment, and this script asks
 * `@typescript-eslint/parser` (through its `typescript-eslint` re-export, a root devDependency) the
 * same way eslint does: `parseForESLint` with `range`, `loc` and `jsx`, and the file's own path so
 * the parser knows the extension it is reading. The comments it reports are the comments eslint
 * builds its directive list from, so a `/` that opens a regular expression, a quote inside one, or a
 * keyword that is a property name cannot be read as a comment by mistake.
 *
 * Usage: `node scripts/js-comments.mjs <file> [<file> ...]`
 *
 * One JSON object per line on stdout, in argument order:
 *
 *     {"path": "...", "comments": [{"line": 3, "text": " eslint-disable ", "block": true}], "error": null}
 *
 * `text` is the comment body without its `//` or `/*` `*\/` markers, the string eslint matches a
 * directive against. A file the parser cannot parse comes back with `error` set and no comments,
 * and the script still exits 0: one unparsable file is that file's verdict, not a reader failure.
 * A failure of the script itself - a missing argument, a package that does not resolve, an
 * unreadable file, a throw outside the parse - is loud: it goes to stderr and exits non-zero, and
 * the Python side refuses to fall back to its character scanner on it, because that scanner is
 * known to miss a directive in three shapes.
 */
import fs from 'node:fs';
import { parser } from 'typescript-eslint';

const paths = process.argv.slice(2);
if (paths.length === 0) {
  console.error('usage: node scripts/js-comments.mjs <file> [<file> ...]');
  process.exit(2);
}

const message = (error) => (error instanceof Error ? error.message : String(error));

for (const path of paths) {
  let source;
  try {
    source = fs.readFileSync(path, 'utf8');
  } catch (error) {
    console.error(`cannot read ${path}: ${message(error)}`);
    process.exit(1);
  }
  let record;
  try {
    const { ast } = parser.parseForESLint(source, { filePath: path, range: true, loc: true, jsx: true });
    const comments = (ast.comments ?? []).map((comment) => ({
      line: comment.loc.start.line,
      text: comment.value,
      block: comment.type === 'Block',
    }));
    record = { path, comments, error: null };
  } catch (error) {
    record = { path, comments: [], error: message(error) };
  }
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

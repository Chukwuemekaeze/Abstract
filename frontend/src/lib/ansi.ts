// Strip ANSI escape sequences (color codes, cursor movement, the progress
// overwrites docker emits) so a build transcript renders as clean text in a
// <pre>. Display only: keep the raw string for the Copy button.
export function stripAnsi(input: string): string {
  // CSI sequences: ESC [ ... final letter. The ESC (\x1b) control character is
  // the whole point here, so the no-control-regex rule does not apply.
  // eslint-disable-next-line no-control-regex
  return input.replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, '')
}

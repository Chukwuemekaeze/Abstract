// Client-side dotenv parser mirroring the backend's parse_dotenv exactly:
// comments and blank lines skipped, optional `export ` prefix stripped, split
// on the first '=', a single matching quote pair stripped from the value, no
// ${VAR} interpolation, last duplicate key wins. Errors are collected per
// line (not thrown) so the paste UI can show them inline.

export interface DotenvParseResult {
  variables: Record<string, string>
  errors: Array<{ line: number; message: string }>
}

export function parseDotenv(text: string): DotenvParseResult {
  const variables: Record<string, string> = {}
  const errors: Array<{ line: number; message: string }> = []

  text.split(/\r?\n/).forEach((rawLine, index) => {
    const lineNumber = index + 1
    let line = rawLine.trim()
    if (!line || line.startsWith('#')) return
    if (line.startsWith('export ')) {
      line = line.slice('export '.length).trimStart()
    }
    const eq = line.indexOf('=')
    if (eq === -1) {
      errors.push({
        line: lineNumber,
        message: `expected KEY=value, got "${line}"`,
      })
      return
    }
    const key = line.slice(0, eq).trim()
    if (!key) {
      errors.push({ line: lineNumber, message: "empty key before '='" })
      return
    }
    let value = line.slice(eq + 1).trim()
    if (
      value.length >= 2 &&
      value[0] === value[value.length - 1] &&
      (value[0] === '"' || value[0] === "'")
    ) {
      value = value.slice(1, -1)
    }
    variables[key] = value
  })

  return { variables, errors }
}

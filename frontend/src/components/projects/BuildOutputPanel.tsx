// Scrollable, wrapping panel for a docker build transcript. ANSI codes are
// stripped for display; the Copy button yields the raw output (ANSI intact)
// so users can paste it into issues or other tools verbatim.

import { useState } from 'react'
import { Check, Copy } from 'lucide-react'

import { stripAnsi } from '@/lib/ansi'

export function BuildOutputPanel({ output }: { output: string }) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    await navigator.clipboard.writeText(output)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={copy}
        className="absolute right-2 top-2 z-10 rounded bg-background/80 p-1 text-muted-foreground hover:text-foreground"
        title="Copy raw output"
      >
        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </button>
      <pre className="max-h-[60vh] overflow-y-auto whitespace-pre-wrap break-all [overflow-wrap:anywhere] rounded bg-muted p-4 font-mono text-xs">
        <code>{stripAnsi(output)}</code>
      </pre>
    </div>
  )
}

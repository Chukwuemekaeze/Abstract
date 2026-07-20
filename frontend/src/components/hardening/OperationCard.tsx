// A single hardening operation: title, description, a status badge, a run/retry
// button, and (on failure) a collapsible panel showing the captured shell output.
//
// The collapsible is a plain local-state toggle plus a <pre>, mirroring the existing
// ServerCard. The disabledReason is surfaced via the native title tooltip. This keeps
// the component dependency free and consistent with the rest of the app.

import { type ReactNode, useState } from 'react'
import { Check, ChevronDown, Loader2, X } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { cn } from '@/lib/utils'

export type OperationStatus = 'idle' | 'running' | 'success' | 'failed'

export interface OperationCardProps {
  title: string
  description: string
  status: OperationStatus
  statusLabel: string
  onRun: () => void
  runLabel: string
  isRetry: boolean
  output: string | null
  disabled?: boolean
  disabledReason?: string
  // Optional controls (e.g. a username input) rendered above the run button.
  children?: ReactNode
}

function StatusBadge({
  status,
  statusLabel,
}: {
  status: OperationStatus
  statusLabel: string
}) {
  if (status === 'running') {
    return (
      <Badge variant="secondary">
        <Loader2 className="mr-1 size-3 animate-spin" />
        Running...
      </Badge>
    )
  }
  if (status === 'success') {
    return (
      <Badge className="bg-green-600 text-white">
        <Check className="mr-1 size-3" />
        {statusLabel}
      </Badge>
    )
  }
  if (status === 'failed') {
    return (
      <Badge className="bg-red-600 text-white">
        <X className="mr-1 size-3" />
        Failed
      </Badge>
    )
  }
  return <Badge variant="outline">{statusLabel}</Badge>
}

export function OperationCard({
  title,
  description,
  status,
  statusLabel,
  onRun,
  runLabel,
  isRetry,
  output,
  disabled = false,
  disabledReason,
  children,
}: OperationCardProps) {
  const hasOutput = status === 'failed' && Boolean(output)
  // Failed operations open the output by default so the error is visible.
  const [showOutput, setShowOutput] = useState(true)

  return (
    <Card
      className={cn(
        status === 'running' && 'animate-pulse border-primary',
        status === 'idle' && 'opacity-90',
      )}
    >
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
        <CardAction>
          <StatusBadge status={status} statusLabel={statusLabel} />
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {children}
        <div title={disabled ? disabledReason : undefined}>
          <Button
            type="button"
            size="sm"
            variant={status === 'failed' ? 'destructive' : 'default'}
            onClick={onRun}
            disabled={disabled || status === 'running'}
          >
            {status === 'running' && (
              <Loader2 className="mr-2 size-4 animate-spin" />
            )}
            {isRetry ? 'Retry' : runLabel}
          </Button>
        </div>

        {hasOutput && (
          <div>
            <button
              type="button"
              onClick={() => setShowOutput((v) => !v)}
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <ChevronDown
                className={cn(
                  'size-3 transition-transform',
                  !showOutput && '-rotate-90',
                )}
              />
              {showOutput ? 'Hide output' : 'Show output'}
            </button>
            {showOutput && (
              <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs">
                <code>{output}</code>
              </pre>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

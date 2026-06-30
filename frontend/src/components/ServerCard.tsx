// A single server row. Shows identity, the (truncated, expandable) fingerprint,
// a colored status badge, and a Run Smoke Test action that exercises the end to end
// SSH path and shows the command output inline.

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage } from '@/api/client'
import {
  type CommandResult,
  type Server,
  type ServerStatus,
  useSmokeTestMutation,
} from '@/api/servers'
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

// Map each status to a badge variant and label. key_mismatch is the alarming one.
const STATUS_META: Record<
  ServerStatus,
  { label: string; className: string }
> = {
  verified: { label: 'verified', className: 'bg-green-600 text-white' },
  pending_verification: {
    label: 'pending',
    className: 'bg-yellow-500 text-black',
  },
  key_mismatch: { label: 'key mismatch', className: 'bg-red-600 text-white' },
}

export function ServerCard({ server }: { server: Server }) {
  // Expand/collapse the long fingerprint string.
  const [showFullFingerprint, setShowFullFingerprint] = useState(false)
  // Last smoke test output, shown in an expandable block under the card.
  const [smokeResult, setSmokeResult] = useState<CommandResult | null>(null)

  const smokeTest = useSmokeTestMutation()
  const status = STATUS_META[server.status]

  const runSmokeTest = async () => {
    setSmokeResult(null)
    try {
      const result = await smokeTest.mutateAsync(server.id)
      setSmokeResult(result)
      toast.success('Smoke test completed.')
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Smoke test failed.'))
    }
  }

  const fingerprint = server.fingerprint_sha256 ?? 'unknown'
  // Truncate to keep the card compact unless the user expands it.
  const fingerprintDisplay =
    showFullFingerprint || fingerprint.length <= 24
      ? fingerprint
      : `${fingerprint.slice(0, 24)}...`

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <Link to={`/servers/${server.id}`} className="hover:underline">
            {server.name}
          </Link>
        </CardTitle>
        <CardDescription>
          {server.username}@{server.host}:{server.port}
        </CardDescription>
        <CardAction>
          <Badge className={status.className}>{status.label}</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <button
          type="button"
          onClick={() => setShowFullFingerprint((v) => !v)}
          className="text-left font-mono text-xs text-muted-foreground hover:text-foreground"
          title="Click to expand or collapse"
        >
          {fingerprintDisplay}
        </button>

        <div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={runSmokeTest}
            // Smoke test only makes sense once the server is verified.
            disabled={server.status !== 'verified' || smokeTest.isPending}
          >
            {smokeTest.isPending && (
              <Loader2 className="mr-2 size-4 animate-spin" />
            )}
            Run smoke test
          </Button>
        </div>

        {/* Inline output of the last smoke test run. */}
        {smokeResult && (
          <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">
            <code>
              {`exit ${smokeResult.exit_status}\n${smokeResult.stdout}${
                smokeResult.stderr ? `\n[stderr]\n${smokeResult.stderr}` : ''
              }`}
            </code>
          </pre>
        )}
      </CardContent>
    </Card>
  )
}

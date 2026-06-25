// The trust on first use checkpoint. We show the host key fingerprint we captured
// during the probe and ask the user to compare it, out of band, against what their
// VPS provider console shows. Confirming means "yes this is really my server",
// which is what authorizes installing the app key over the password session.

import { useState } from 'react'
import { Check, Copy } from 'lucide-react'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

interface FingerprintConfirmProps {
  fingerprint: string
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
}

export function FingerprintConfirm({
  fingerprint,
  onConfirm,
  onCancel,
  busy = false,
}: FingerprintConfirmProps) {
  // Local, transient "copied" state just to flip the button icon for feedback.
  const [copied, setCopied] = useState(false)

  const copyFingerprint = async () => {
    await navigator.clipboard.writeText(fingerprint)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="flex flex-col gap-4">
      <Alert>
        <AlertTitle>Verify the host key fingerprint</AlertTitle>
        <AlertDescription>
          Compare this fingerprint to the one shown in your VPS provider's console
          before continuing. They must match.
        </AlertDescription>
      </Alert>

      {/* Monospace box so every character is unambiguous for visual comparison. */}
      <div className="flex items-center gap-2 rounded-md border bg-muted p-3">
        <code className="flex-1 break-all font-mono text-sm">{fingerprint}</code>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={copyFingerprint}
          aria-label="Copy fingerprint"
        >
          {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
        </Button>
      </div>

      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="button" onClick={onConfirm} disabled={busy}>
          Fingerprint matches, install key
        </Button>
      </div>
    </div>
  )
}

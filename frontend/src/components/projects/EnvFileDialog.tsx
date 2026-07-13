// Create/edit dialog for one env file: path field, key-value rows, and a
// "Paste from .env" textarea that runs the client-side dotenv parser.
//
// Saved values never come back from the API, so in edit mode every existing
// value field is empty with a placeholder. Leaving it empty preserves the
// stored value; typing sends the new one (set_variables upsert); deleting a
// row sends the key in remove_keys.
//
// The dialog mounts only while open, and the form mounts only once the keys
// are known (create mode: immediately; edit mode: after the detail loads), so
// all state initializes from props with no reset effects.

import { useState } from 'react'
import { ClipboardPaste, Eye, EyeOff, Loader2, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage } from '@/api/client'
import {
  useCreateEnvFileMutation,
  useEnvFile,
  useUpdateEnvFileMutation,
  type EnvFileListItem,
} from '@/api/env-files'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { parseDotenv } from '@/lib/dotenv'

interface Row {
  rowId: number
  key: string
  value: string
  // True when the key exists on the server; its empty value means "keep".
  existing: boolean
}

let nextRowId = 1
const newRow = (key = '', value = '', existing = false): Row => ({
  rowId: nextRowId++,
  key,
  value,
  existing,
})

const HIDDEN_PLACEHOLDER = '•••••••• (click to edit)'

export function EnvFileDialog({
  projectId,
  envFile,
  open,
  onOpenChange,
}: {
  projectId: string
  // null = create mode; an existing file = edit mode.
  envFile: EnvFileListItem | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  if (!open) return null
  return (
    <EnvFileDialogOpen
      projectId={projectId}
      envFile={envFile}
      onOpenChange={onOpenChange}
    />
  )
}

function EnvFileDialogOpen({
  projectId,
  envFile,
  onOpenChange,
}: {
  projectId: string
  envFile: EnvFileListItem | null
  onOpenChange: (open: boolean) => void
}) {
  const isEdit = envFile !== null
  const detail = useEnvFile(projectId, isEdit ? envFile.id : null)
  const loading = isEdit && !detail.data

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit env file' : 'Add env file'}</DialogTitle>
          <DialogDescription>
            Values are encrypted at rest and never shown again after saving.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="size-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <EnvFileForm
            projectId={projectId}
            envFile={envFile}
            initialKeys={detail.data?.keys ?? []}
            onOpenChange={onOpenChange}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function EnvFileForm({
  projectId,
  envFile,
  initialKeys,
  onOpenChange,
}: {
  projectId: string
  envFile: EnvFileListItem | null
  initialKeys: string[]
  onOpenChange: (open: boolean) => void
}) {
  const isEdit = envFile !== null
  const createMutation = useCreateEnvFileMutation(projectId)
  const updateMutation = useUpdateEnvFileMutation(projectId, envFile?.id ?? '')
  const isPending = createMutation.isPending || updateMutation.isPending

  const [path, setPath] = useState(() => envFile?.path ?? '')
  const [rows, setRows] = useState<Row[]>(() =>
    initialKeys.length > 0
      ? initialKeys.map((key) => newRow(key, '', true))
      : [newRow()],
  )
  const [removedKeys, setRemovedKeys] = useState<string[]>([])
  const [visibleRows, setVisibleRows] = useState<Set<number>>(new Set())
  const [showPaste, setShowPaste] = useState(false)
  const [pasteText, setPasteText] = useState('')
  const [pasteErrors, setPasteErrors] = useState<
    Array<{ line: number; message: string }>
  >([])
  const [formError, setFormError] = useState<string | null>(null)

  const updateRow = (rowId: number, patch: Partial<Row>) => {
    setRows((rs) => rs.map((r) => (r.rowId === rowId ? { ...r, ...patch } : r)))
  }

  const deleteRow = (row: Row) => {
    setRows((rs) => rs.filter((r) => r.rowId !== row.rowId))
    if (row.existing) setRemovedKeys((ks) => [...ks, row.key])
  }

  const toggleVisible = (rowId: number) => {
    setVisibleRows((s) => {
      const next = new Set(s)
      if (next.has(rowId)) next.delete(rowId)
      else next.add(rowId)
      return next
    })
  }

  const applyPaste = () => {
    const { variables, errors } = parseDotenv(pasteText)
    setPasteErrors(errors)
    if (errors.length > 0) return
    setRows((rs) => {
      const next = [...rs.filter((r) => r.key.trim() !== '' || r.existing)]
      for (const [key, value] of Object.entries(variables)) {
        const match = next.find((r) => r.key === key)
        if (match) match.value = value
        else next.push(newRow(key, value))
      }
      return next
    })
    setShowPaste(false)
    setPasteText('')
  }

  const save = async () => {
    setFormError(null)
    const trimmedPath = path.trim()
    if (!trimmedPath) {
      setFormError('Path is required, e.g. ".env" or "backend/.env".')
      return
    }
    const activeRows = rows.filter((r) => r.key.trim() !== '')
    const duplicate = activeRows.find(
      (r, i) => activeRows.findIndex((o) => o.key.trim() === r.key.trim()) !== i,
    )
    if (duplicate) {
      setFormError(`Duplicate key "${duplicate.key.trim()}".`)
      return
    }

    try {
      if (isEdit) {
        const setVariables: Record<string, string> = {}
        for (const row of activeRows) {
          // Existing keys with an untouched (empty) value keep their stored
          // value by being omitted from the request.
          if (!row.existing || row.value !== '') {
            setVariables[row.key.trim()] = row.value
          }
        }
        await updateMutation.mutateAsync({
          ...(trimmedPath !== envFile.path ? { path: trimmedPath } : {}),
          set_variables: setVariables,
          remove_keys: removedKeys,
        })
        toast.success('Env file updated.')
      } else {
        const variables: Record<string, string> = {}
        for (const row of activeRows) variables[row.key.trim()] = row.value
        await createMutation.mutateAsync({ path: trimmedPath, variables })
        toast.success('Env file created.')
      }
      onOpenChange(false)
    } catch (err) {
      setFormError(extractErrorMessage(err, 'Saving the env file failed'))
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="env-file-path">Path</Label>
        <Input
          id="env-file-path"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder=".env or backend/.env"
          disabled={isPending}
        />
        <p className="text-xs text-muted-foreground">
          Relative to the project directory on your server.
        </p>
      </div>

      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setShowPaste((v) => !v)}
          disabled={isPending}
        >
          <ClipboardPaste className="mr-1.5 size-3.5" />
          Paste from .env
        </Button>
        {showPaste && (
          <div className="mt-2 flex flex-col gap-2">
            <textarea
              value={pasteText}
              onChange={(e) => setPasteText(e.target.value)}
              rows={6}
              placeholder={'KEY=value\nANOTHER=value'}
              className="w-full rounded-md border bg-transparent p-2 font-mono text-xs"
            />
            {pasteErrors.map((err) => (
              <p key={err.line} className="text-xs text-red-600">
                Line {err.line}: {err.message}
              </p>
            ))}
            <div className="flex gap-2">
              <Button type="button" size="sm" onClick={applyPaste}>
                Apply
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => {
                  setShowPaste(false)
                  setPasteErrors([])
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}
      </div>

      <div className="flex max-h-72 flex-col gap-2 overflow-y-auto">
        {rows.map((row) => (
          <div key={row.rowId} className="flex items-center gap-2">
            <Input
              value={row.key}
              onChange={(e) => updateRow(row.rowId, { key: e.target.value })}
              placeholder="KEY"
              className="w-2/5 font-mono text-xs"
              disabled={isPending || row.existing}
            />
            <div className="relative flex-1">
              <Input
                type={visibleRows.has(row.rowId) ? 'text' : 'password'}
                value={row.value}
                onChange={(e) => updateRow(row.rowId, { value: e.target.value })}
                placeholder={row.existing ? HIDDEN_PLACEHOLDER : 'value'}
                className="pr-8 font-mono text-xs"
                disabled={isPending}
              />
              <button
                type="button"
                onClick={() => toggleVisible(row.rowId)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                title={visibleRows.has(row.rowId) ? 'Hide value' : 'Show value'}
              >
                {visibleRows.has(row.rowId) ? (
                  <EyeOff className="size-3.5" />
                ) : (
                  <Eye className="size-3.5" />
                )}
              </button>
            </div>
            <button
              type="button"
              onClick={() => deleteRow(row)}
              className="text-muted-foreground hover:text-red-600"
              title="Remove variable"
              disabled={isPending}
            >
              <Trash2 className="size-4" />
            </button>
          </div>
        ))}
        <div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setRows((rs) => [...rs, newRow()])}
            disabled={isPending}
          >
            <Plus className="mr-1 size-3.5" />
            Add variable
          </Button>
        </div>
      </div>

      {formError && <p className="text-sm text-red-600">{formError}</p>}

      <div className="flex justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          onClick={() => onOpenChange(false)}
          disabled={isPending}
        >
          Cancel
        </Button>
        <Button type="button" onClick={save} disabled={isPending}>
          {isPending && <Loader2 className="mr-2 size-4 animate-spin" />}
          Save
        </Button>
      </div>
    </div>
  )
}

// Env files list on the project card: file rows (path + variable count) that
// open the edit dialog, a delete-with-confirmation per row, and an add button.
// Values are never displayed anywhere; the API only exposes keys and counts.

import { useState } from 'react'
import { FileLock2, Loader2, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage } from '@/api/client'
import {
  useDeleteEnvFileMutation,
  useEnvFiles,
  type EnvFileListItem,
} from '@/api/env-files'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { EnvFileDialog } from '@/components/projects/EnvFileDialog'

export function EnvFilesSection({ projectId }: { projectId: string }) {
  const envFiles = useEnvFiles(projectId)
  const deleteMutation = useDeleteEnvFileMutation(projectId)

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<EnvFileListItem | null>(null)
  const [confirmFile, setConfirmFile] = useState<EnvFileListItem | null>(null)

  const openCreate = () => {
    setEditing(null)
    setDialogOpen(true)
  }

  const openEdit = (file: EnvFileListItem) => {
    setEditing(file)
    setDialogOpen(true)
  }

  const confirmDelete = async () => {
    if (!confirmFile) return
    try {
      await deleteMutation.mutateAsync(confirmFile.id)
      toast.success(`Deleted ${confirmFile.path}.`)
      setConfirmFile(null)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Deleting the env file failed'))
    }
  }

  const files = envFiles.data ?? []
  // The user manages a root .env themselves; drives the "auto-generated" hint.
  const hasRootEnv = files.some((file) => file.path === '.env')

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-semibold uppercase tracking-[0.06em] text-text-dim">
        Environment
      </p>

      {files.length === 0 ? (
        <div className="flex flex-col items-start gap-2">
          <p className="text-xs text-muted-foreground">
            No environment variables set. Add any variables your app needs to
            run.
          </p>
          <Button type="button" size="sm" onClick={openCreate}>
            Add env file
          </Button>
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-1">
            {files.map((file) => (
              <div
                key={file.id}
                className="flex items-center justify-between rounded-md border px-2 py-1.5"
              >
                <button
                  type="button"
                  onClick={() => openEdit(file)}
                  className="flex min-w-0 items-center gap-2 text-left hover:underline"
                  title="Edit variables"
                >
                  <FileLock2 className="size-3.5 shrink-0 text-muted-foreground" />
                  <span className="truncate font-mono text-xs">{file.path}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {file.variable_count}{' '}
                    {file.variable_count === 1 ? 'variable' : 'variables'}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmFile(file)}
                  className="text-muted-foreground hover:text-red-600"
                  title="Delete env file"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
          {!hasRootEnv && (
            <p className="text-xs text-muted-foreground">
              A root .env with all variables is auto-generated on start so
              compose ${'{VAR}'} substitution works.
            </p>
          )}
          <div>
            <Button type="button" size="sm" variant="outline" onClick={openCreate}>
              + Add env file
            </Button>
          </div>
        </>
      )}

      <EnvFileDialog
        projectId={projectId}
        envFile={editing}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
      />

      <Dialog
        open={confirmFile !== null}
        onOpenChange={(open) => {
          if (!open && !deleteMutation.isPending) setConfirmFile(null)
        }}
      >
        <DialogContent className="max-w-sm" showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>Delete env file?</DialogTitle>
            <DialogDescription>
              <span className="font-mono">{confirmFile?.path}</span> and its
              variables will be permanently removed. This can't be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirmFile(null)}
              disabled={deleteMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              onClick={confirmDelete}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending && (
                <Loader2 className="mr-2 size-4 animate-spin" />
              )}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

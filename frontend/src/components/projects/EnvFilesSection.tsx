// Env files list on the project card: file rows (path + variable count) that
// open the edit dialog, a two-click delete per row, and an add button. Values
// are never displayed anywhere; the API only exposes keys and counts.

import { useEffect, useState } from 'react'
import { FileLock2, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage } from '@/api/client'
import {
  useDeleteEnvFileMutation,
  useEnvFiles,
  useHasRootEnvFile,
  type EnvFileListItem,
} from '@/api/env-files'
import { Button } from '@/components/ui/button'
import { EnvFileDialog } from '@/components/projects/EnvFileDialog'

export function EnvFilesSection({ projectId }: { projectId: string }) {
  const envFiles = useEnvFiles(projectId)
  const hasRootEnv = useHasRootEnvFile(projectId)
  const deleteMutation = useDeleteEnvFileMutation(projectId)

  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<EnvFileListItem | null>(null)
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)

  // Two-click delete: the confirm state expires after a moment.
  useEffect(() => {
    if (!pendingDeleteId) return
    const timer = setTimeout(() => setPendingDeleteId(null), 3000)
    return () => clearTimeout(timer)
  }, [pendingDeleteId])

  const openCreate = () => {
    setEditing(null)
    setDialogOpen(true)
  }

  const openEdit = (file: EnvFileListItem) => {
    setEditing(file)
    setDialogOpen(true)
  }

  const onDeleteClick = async (file: EnvFileListItem) => {
    if (pendingDeleteId !== file.id) {
      setPendingDeleteId(file.id)
      return
    }
    setPendingDeleteId(null)
    try {
      await deleteMutation.mutateAsync(file.id)
      toast.success(`Deleted ${file.path}.`)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Deleting the env file failed'))
    }
  }

  const files = envFiles.data ?? []

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-medium text-muted-foreground">Environment</p>

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
                  onClick={() => onDeleteClick(file)}
                  className={
                    pendingDeleteId === file.id
                      ? 'text-red-600'
                      : 'text-muted-foreground hover:text-red-600'
                  }
                  title={
                    pendingDeleteId === file.id
                      ? 'Click again to delete permanently'
                      : 'Delete env file'
                  }
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
    </div>
  )
}

// Advanced project settings. v1 has a single field: compose_file_path, for
// repos whose compose file is not one of the standard names. Blank clears the
// override so detection falls back to the defaults.
//
// The dialog mounts only while open, so the field initializes fresh from the
// project on every open with no reset effect.

import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage } from '@/api/client'
import { useUpdateProjectMutation, type Project } from '@/api/projects'
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

export function ProjectSettingsDialog({
  project,
  open,
  onOpenChange,
}: {
  project: Project
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  if (!open) return null
  return (
    <SettingsDialogOpen project={project} onOpenChange={onOpenChange} />
  )
}

function SettingsDialogOpen({
  project,
  onOpenChange,
}: {
  project: Project
  onOpenChange: (open: boolean) => void
}) {
  const update = useUpdateProjectMutation(project.id)
  const [composeFilePath, setComposeFilePath] = useState(
    () => project.compose_file_path ?? '',
  )

  const save = async () => {
    try {
      await update.mutateAsync({
        compose_file_path: composeFilePath.trim() || null,
      })
      toast.success('Settings saved.')
      onOpenChange(false)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Saving settings failed'))
    }
  }

  return (
    <Dialog open onOpenChange={(v) => !update.isPending && onOpenChange(v)}>
      <DialogContent showCloseButton={!update.isPending}>
        <DialogHeader>
          <DialogTitle>Project settings</DialogTitle>
          <DialogDescription>Advanced options for {project.name}.</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="compose-file-path">Compose file path</Label>
          <Input
            id="compose-file-path"
            value={composeFilePath}
            onChange={(e) => setComposeFilePath(e.target.value)}
            placeholder="docker-compose.prod.yml"
            disabled={update.isPending}
          />
          <p className="text-xs text-muted-foreground">
            Leave blank to use standard docker-compose.yml. Set this only if
            your compose file has a different name like{' '}
            <code>docker-compose.prod.yml</code>.
          </p>
        </div>

        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={update.isPending}
          >
            Cancel
          </Button>
          <Button type="button" onClick={save} disabled={update.isPending}>
            {update.isPending && <Loader2 className="mr-2 size-4 animate-spin" />}
            Save
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

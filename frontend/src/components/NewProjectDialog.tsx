// The New Project dialog: name, server, GitHub repo, submit. Renders per step
// from the Zustand state machine:
//   form -> submitting -> done (auto close) / failed (retry keeps field values)
//
// The create call is slow by design (the backend registers a deploy key and
// clones the repo before responding), so the submitting step blocks closing.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, ChevronsUpDown, Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractErrorMessage, extractHardeningError } from '@/api/client'
import { useCreateProjectMutation, useGithubRepos } from '@/api/projects'
import { useServers } from '@/api/servers'
import { Button } from '@/components/ui/button'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { relativeTime } from '@/lib/relativeTime'
import { useNewProjectStore } from '@/store/newProjectStore'

export function NewProjectDialog() {
  const {
    step,
    formData,
    initialServerId,
    error,
    errorOutput,
    close,
    setFormData,
    setStep,
    setError,
  } = useNewProjectStore()

  const [repoPickerOpen, setRepoPickerOpen] = useState(false)

  const servers = useServers()
  const repos = useGithubRepos()
  const createProject = useCreateProjectMutation()

  const isOpen = step !== 'idle'
  const isSubmitting = step === 'submitting'

  // Only verified and hardened servers can host projects.
  const eligibleServers = (servers.data ?? []).filter(
    (s) => s.status === 'verified' && s.sudo_user_name !== null,
  )

  // On the done step, auto close after a short success pause.
  useEffect(() => {
    if (step !== 'done') return
    const timer = setTimeout(() => close(), 2000)
    return () => clearTimeout(timer)
  }, [step, close])

  const canSubmit =
    formData.name.trim().length > 0 &&
    formData.serverId !== '' &&
    formData.repoId !== null &&
    !isSubmitting

  const handleSubmit = async () => {
    if (!canSubmit || formData.repoId === null) return
    setStep('submitting')
    setError(null)
    try {
      await createProject.mutateAsync({
        name: formData.name.trim(),
        server_id: formData.serverId,
        github_repo_id: formData.repoId,
        github_repo_full_name: formData.repoFullName,
      })
      setStep('done')
      toast.success('Project created')
    } catch (err) {
      // Clone failures return a structured detail { message, captured_output }
      // like the hardening endpoints; plain string details fall through.
      const { message, output } = extractHardeningError(
        err,
        'Project creation failed.',
      )
      setError(message, output)
      setStep('failed')
    }
  }

  // Retry returns to the form with the previous field values preserved.
  const handleRetry = () => {
    setError(null)
    setStep('form')
  }

  // The dialog must not be dismissable while the backend is mid-provision.
  const handleOpenChange = (open: boolean) => {
    if (!open && !isSubmitting) close()
  }

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent showCloseButton={!isSubmitting}>
        <DialogHeader>
          <DialogTitle>New project</DialogTitle>
          <DialogDescription>
            Pick a repo and a server; Abstract sets up a dedicated deploy key
            and clones the repo.
          </DialogDescription>
        </DialogHeader>

        {/* Step: form */}
        {step === 'form' && (
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              handleSubmit()
            }}
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="project-name">Project name</Label>
              <Input
                id="project-name"
                value={formData.name}
                maxLength={100}
                onChange={(e) => setFormData({ name: e.target.value })}
                placeholder="my-app"
                required
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label>Server</Label>
              {eligibleServers.length === 0 ? (
                <p className="text-muted-foreground text-sm">
                  You need a verified, hardened server first.{' '}
                  <Link to="/" className="underline" onClick={close}>
                    Add or harden a server.
                  </Link>
                </p>
              ) : (
                <Select
                  value={formData.serverId}
                  onValueChange={(value) => setFormData({ serverId: value })}
                  disabled={initialServerId !== null}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select a server" />
                  </SelectTrigger>
                  <SelectContent>
                    {eligibleServers.map((server) => (
                      <SelectItem key={server.id} value={server.id}>
                        {server.name} ({server.host})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>

            <div className="flex flex-col gap-2">
              <Label>GitHub repo</Label>
              <Popover open={repoPickerOpen} onOpenChange={setRepoPickerOpen}>
                <PopoverTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    role="combobox"
                    aria-expanded={repoPickerOpen}
                    className="w-full justify-between font-normal"
                  >
                    {formData.repoFullName || 'Select a repo'}
                    <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent
                  className="w-[var(--radix-popover-trigger-width)] p-0"
                  align="start"
                >
                  <Command>
                    <CommandInput placeholder="Search repos..." />
                    <CommandList>
                      {repos.isLoading && (
                        <div className="flex items-center justify-center gap-2 py-6 text-sm">
                          <Loader2 className="size-4 animate-spin" />
                          Loading repos...
                        </div>
                      )}
                      {repos.isError && (
                        <p className="text-destructive px-3 py-6 text-sm">
                          {extractErrorMessage(
                            repos.error,
                            'Could not load repos.',
                          )}
                        </p>
                      )}
                      {!repos.isLoading && !repos.isError && (
                        <>
                          <CommandEmpty>No repos found.</CommandEmpty>
                          <CommandGroup>
                            {(repos.data ?? []).map((repo) => (
                              <CommandItem
                                key={repo.id}
                                value={repo.full_name}
                                onSelect={() => {
                                  setFormData({
                                    repoId: repo.id,
                                    repoFullName: repo.full_name,
                                  })
                                  setRepoPickerOpen(false)
                                }}
                              >
                                <Check
                                  className={cn(
                                    'size-4',
                                    formData.repoId === repo.id
                                      ? 'opacity-100'
                                      : 'opacity-0',
                                  )}
                                />
                                <div className="flex flex-col">
                                  <span>{repo.full_name}</span>
                                  <span className="text-muted-foreground text-xs">
                                    pushed {relativeTime(repo.pushed_at)}
                                  </span>
                                </div>
                              </CommandItem>
                            ))}
                          </CommandGroup>
                        </>
                      )}
                    </CommandList>
                  </Command>
                </PopoverContent>
              </Popover>
            </div>

            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={close}>
                Cancel
              </Button>
              <Button type="submit" disabled={!canSubmit}>
                Create
              </Button>
            </div>
          </form>
        )}

        {/* Step: submitting (deploy key + clone; can take 10-30 seconds) */}
        {step === 'submitting' && (
          <div className="flex items-center gap-3 py-8">
            <Loader2 className="size-5 animate-spin" />
            <span>Setting up your project...</span>
          </div>
        )}

        {/* Step: done */}
        {step === 'done' && (
          <div className="py-8 text-center">
            <p className="text-lg font-medium">Project created</p>
            <p className="text-muted-foreground text-sm">
              This dialog will close shortly.
            </p>
          </div>
        )}

        {/* Step: failed */}
        {step === 'failed' && (
          <div className="flex flex-col gap-4 py-4">
            <p className="text-destructive text-sm">{error}</p>
            {errorOutput && (
              <pre className="max-h-48 overflow-auto rounded-md bg-muted p-3 text-xs">
                <code>{errorOutput}</code>
              </pre>
            )}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={close}>
                Close
              </Button>
              <Button type="button" onClick={handleRetry}>
                Retry
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

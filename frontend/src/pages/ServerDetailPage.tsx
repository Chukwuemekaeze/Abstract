// Server detail page. Shows the server identity and a stack of hardening operations,
// each an OperationCard wired to its mutation. Quick harden runs the whole sequence.
//
// The Reboot operation is special: after it fires, the page enters a "rebooting"
// state, disables the other operations, and polls POST /ping every 5s (up to 5 min)
// until the box answers, then re-enables everything.

import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { toast } from 'sonner'

import { extractHardeningError } from '@/api/client'
import {
  type Server,
  type ServerStatus,
  useConfigureFirewallMutation,
  useCreateSudoUserMutation,
  useCreateSwapMutation,
  useDisablePasswordAuthMutation,
  useDisableRootLoginMutation,
  useInstallBasePackagesMutation,
  useInstallDockerMutation,
  useRebootMutation,
  useServer,
  useServerPing,
  useUpdateSystemMutation,
} from '@/api/servers'
import { useProjectsByServer } from '@/api/projects'
import { Header } from '@/components/Header'
import { NewProjectDialog } from '@/components/NewProjectDialog'
import { ProjectCard } from '@/components/ProjectCard'
import {
  OperationCard,
  type OperationStatus,
} from '@/components/hardening/OperationCard'
import { QuickHardenSection } from '@/components/hardening/QuickHardenSection'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useNewProjectStore } from '@/store/newProjectStore'

const STATUS_META: Record<ServerStatus, { label: string; className: string }> = {
  verified: { label: 'verified', className: 'bg-green-600 text-white' },
  pending_verification: { label: 'pending', className: 'bg-yellow-500 text-black' },
  key_mismatch: { label: 'key mismatch', className: 'bg-red-600 text-white' },
}

const REBOOT_TIMEOUT_MS = 5 * 60 * 1000

export function ServerDetailPage() {
  const { id } = useParams<{ id: string }>()
  const { data: server, isLoading, isError } = useServer(id)

  return (
    <>
      <Header />
      <div className="mx-auto max-w-3xl px-6 py-10">
        <Link
          to="/"
          className="mb-6 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back to servers
        </Link>

        {isLoading && (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Loading server...
          </div>
        )}
        {isError && <p className="text-destructive">Could not load this server.</p>}
        {server && <ServerDetail server={server} />}
      </div>
    </>
  )
}

function ServerDetail({ server }: { server: Server }) {
  // Per-operation captured error output (keyed by operation name).
  const [errors, setErrors] = useState<Record<string, string | null>>({})
  // Operations with no persistent boolean column (update_system, base packages) that
  // succeeded in this session, so the card can show success without a server field.
  const [succeeded, setSucceeded] = useState<Record<string, boolean>>({})

  const [showFullFingerprint, setShowFullFingerprint] = useState(false)

  // Reboot state.
  const [rebooting, setRebooting] = useState(false)
  const [rebootError, setRebootError] = useState<string | null>(null)
  const rebootDeadline = useRef<number | null>(null)

  const updateSystem = useUpdateSystemMutation()
  const installBase = useInstallBasePackagesMutation()
  const installDocker = useInstallDockerMutation()
  const createSudoUser = useCreateSudoUserMutation()
  const disableRoot = useDisableRootLoginMutation()
  const disablePasswordAuth = useDisablePasswordAuthMutation()
  const configureFirewall = useConfigureFirewallMutation()
  const createSwap = useCreateSwapMutation()
  const reboot = useRebootMutation()

  const ping = useServerPing(server.id, {
    enabled: rebooting,
    refetchInterval: 5000,
  })

  // Stop rebooting when the box answers a ping.
  useEffect(() => {
    if (rebooting && ping.isSuccess) {
      setRebooting(false)
      rebootDeadline.current = null
      toast.success('Server is back online.')
    }
  }, [rebooting, ping.isSuccess])

  // Give up after the timeout window.
  useEffect(() => {
    if (!rebooting) return
    if (rebootDeadline.current === null) {
      rebootDeadline.current = Date.now() + REBOOT_TIMEOUT_MS
    }
    const timer = setInterval(() => {
      if (rebootDeadline.current !== null && Date.now() > rebootDeadline.current) {
        setRebooting(false)
        rebootDeadline.current = null
        setRebootError(
          "Server has not come back online. Check your VPS provider's console.",
        )
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [rebooting])

  const anyRunning =
    updateSystem.isPending ||
    installBase.isPending ||
    installDocker.isPending ||
    createSudoUser.isPending ||
    disableRoot.isPending ||
    disablePasswordAuth.isPending ||
    configureFirewall.isPending ||
    createSwap.isPending ||
    reboot.isPending

  const lockedForReboot = rebooting

  // Build a run handler for a standard operation.
  function runner(
    key: string,
    title: string,
    mutateAsync: (args: { serverId: string }) => Promise<Server>,
  ) {
    return async () => {
      setErrors((e) => ({ ...e, [key]: null }))
      try {
        await mutateAsync({ serverId: server.id })
        setSucceeded((s) => ({ ...s, [key]: true }))
        toast.success(`${title} succeeded.`)
      } catch (err) {
        const { message, output } = extractHardeningError(err)
        setErrors((e) => ({ ...e, [key]: output ?? message }))
        toast.error(message)
      }
    }
  }

  function deriveStatus(
    key: string,
    pending: boolean,
    successField: boolean,
  ): OperationStatus {
    if (pending) return 'running'
    if (errors[key]) return 'failed'
    if (successField || succeeded[key]) return 'success'
    return 'idle'
  }

  const fingerprint = server.fingerprint_sha256 ?? 'unknown'
  const fingerprintDisplay =
    showFullFingerprint || fingerprint.length <= 24
      ? fingerprint
      : `${fingerprint.slice(0, 24)}...`

  const status = STATUS_META[server.status]
  const disabledReasonReboot = 'Server is rebooting.'

  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <div>
        <div className="flex items-center justify-between gap-4">
          <h1 className="text-2xl font-bold">{server.name}</h1>
          <Badge className={status.className}>{status.label}</Badge>
        </div>
        <p className="text-muted-foreground text-sm">
          {server.username}@{server.host}:{server.port}
        </p>
        <button
          type="button"
          onClick={() => setShowFullFingerprint((v) => !v)}
          className="mt-2 text-left font-mono text-xs text-muted-foreground hover:text-foreground"
          title="Click to expand or collapse"
        >
          {fingerprintDisplay}
        </button>
      </div>

      {/* Reboot banners */}
      {rebooting && (
        <Alert>
          <Loader2 className="size-4 animate-spin" />
          <AlertTitle>Server is rebooting.</AlertTitle>
          <AlertDescription>This usually takes 30 to 60 seconds.</AlertDescription>
        </Alert>
      )}
      {rebootError && !rebooting && (
        <Alert variant="destructive">
          <AlertTitle>Reboot did not complete</AlertTitle>
          <AlertDescription>{rebootError}</AlertDescription>
        </Alert>
      )}

      <QuickHardenSection server={server} disabled={anyRunning || lockedForReboot} />

      <ProjectsSection server={server} />

      <div className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Operations</h2>

        <OperationCard
          title="Update system"
          description="apt-get update and upgrade with noninteractive frontend."
          status={deriveStatus(
            'update_system',
            updateSystem.isPending,
            Boolean(server.last_system_update_at),
          )}
          statusLabel={
            server.last_system_update_at
              ? `Updated ${new Date(server.last_system_update_at).toLocaleDateString()}`
              : 'Not run'
          }
          onRun={runner('update_system', 'Update system', updateSystem.mutateAsync)}
          runLabel="Run"
          isRetry={Boolean(errors['update_system'])}
          output={errors['update_system'] ?? null}
          disabled={anyRunning || lockedForReboot}
          disabledReason={lockedForReboot ? disabledReasonReboot : undefined}
        />

        <OperationCard
          title="Install base packages"
          description="git, certbot, ufw, curl, ca-certificates."
          status={deriveStatus(
            'install_base_packages',
            installBase.isPending,
            server.base_packages_installed,
          )}
          statusLabel={server.base_packages_installed ? 'Installed' : 'Not run'}
          onRun={runner(
            'install_base_packages',
            'Install base packages',
            installBase.mutateAsync,
          )}
          runLabel="Run"
          isRetry={Boolean(errors['install_base_packages'])}
          output={errors['install_base_packages'] ?? null}
          disabled={anyRunning || lockedForReboot}
          disabledReason={lockedForReboot ? disabledReasonReboot : undefined}
        />

        <OperationCard
          title="Install Docker"
          description="Official get.docker.com installer (idempotent)."
          status={deriveStatus(
            'install_docker',
            installDocker.isPending,
            server.docker_installed,
          )}
          statusLabel={server.docker_installed ? 'Installed' : 'Not run'}
          onRun={runner('install_docker', 'Install Docker', installDocker.mutateAsync)}
          runLabel="Run"
          isRetry={Boolean(errors['install_docker'])}
          output={errors['install_docker'] ?? null}
          disabled={anyRunning || lockedForReboot}
          disabledReason={lockedForReboot ? disabledReasonReboot : undefined}
        />

        <OperationCard
          title="Create sudo user"
          description="Non-root user with passwordless sudo and the app deploy key."
          status={deriveStatus(
            'create_sudo_user',
            createSudoUser.isPending,
            Boolean(server.sudo_user_name),
          )}
          statusLabel={
            server.sudo_user_name ? `Created (${server.sudo_user_name})` : 'Not run'
          }
          onRun={async () => {
            const key = 'create_sudo_user'
            setErrors((e) => ({ ...e, [key]: null }))
            try {
              // Reuse the existing sudo user name if set, otherwise default to deploy.
              await createSudoUser.mutateAsync({
                serverId: server.id,
                body: { sudo_user_name: server.sudo_user_name ?? 'deploy' },
              })
              toast.success('Sudo user created.')
            } catch (err) {
              const { message, output } = extractHardeningError(err)
              setErrors((e) => ({ ...e, [key]: output ?? message }))
              toast.error(message)
            }
          }}
          runLabel="Run"
          isRetry={Boolean(errors['create_sudo_user'])}
          output={errors['create_sudo_user'] ?? null}
          disabled={anyRunning || lockedForReboot}
          disabledReason={lockedForReboot ? disabledReasonReboot : undefined}
        />

        <OperationCard
          title="Disable root login"
          description="Set PermitRootLogin no and reload sshd."
          status={deriveStatus(
            'disable_root_login',
            disableRoot.isPending,
            server.root_login_disabled,
          )}
          statusLabel={server.root_login_disabled ? 'Disabled' : 'Not run'}
          onRun={runner(
            'disable_root_login',
            'Disable root login',
            disableRoot.mutateAsync,
          )}
          runLabel="Run"
          isRetry={Boolean(errors['disable_root_login'])}
          output={errors['disable_root_login'] ?? null}
          disabled={anyRunning || lockedForReboot || !server.sudo_user_name}
          disabledReason={
            lockedForReboot
              ? disabledReasonReboot
              : !server.sudo_user_name
                ? 'Create a sudo user first.'
                : undefined
          }
        />

        <OperationCard
          title="Disable password authentication"
          description="Set PasswordAuthentication no and reload sshd. Safe: key login already works."
          status={deriveStatus(
            'disable_password_auth',
            disablePasswordAuth.isPending,
            server.password_auth_disabled,
          )}
          statusLabel={server.password_auth_disabled ? 'Disabled' : 'Not run'}
          onRun={runner(
            'disable_password_auth',
            'Disable password authentication',
            disablePasswordAuth.mutateAsync,
          )}
          runLabel="Run"
          isRetry={Boolean(errors['disable_password_auth'])}
          output={errors['disable_password_auth'] ?? null}
          disabled={anyRunning || lockedForReboot}
          disabledReason={lockedForReboot ? disabledReasonReboot : undefined}
        />

        <OperationCard
          title="Configure firewall"
          description="Allow OpenSSH, 80, 443 and enable UFW."
          status={deriveStatus(
            'configure_firewall',
            configureFirewall.isPending,
            server.firewall_enabled,
          )}
          statusLabel={server.firewall_enabled ? 'Active' : 'Not run'}
          onRun={runner(
            'configure_firewall',
            'Configure firewall',
            configureFirewall.mutateAsync,
          )}
          runLabel="Run"
          isRetry={Boolean(errors['configure_firewall'])}
          output={errors['configure_firewall'] ?? null}
          disabled={anyRunning || lockedForReboot}
          disabledReason={lockedForReboot ? disabledReasonReboot : undefined}
        />

        <OperationCard
          title="Create swap"
          description="Swap file sized at 25% of RAM (512MB floor)."
          status={deriveStatus(
            'create_swap',
            createSwap.isPending,
            server.swap_configured,
          )}
          statusLabel={server.swap_configured ? 'Configured' : 'Not run'}
          onRun={runner('create_swap', 'Create swap', createSwap.mutateAsync)}
          runLabel="Run"
          isRetry={Boolean(errors['create_swap'])}
          output={errors['create_swap'] ?? null}
          disabled={anyRunning || lockedForReboot}
          disabledReason={lockedForReboot ? disabledReasonReboot : undefined}
        />

        <OperationCard
          title="Reboot"
          description="Reboot the server and wait for it to come back online."
          status={
            rebooting || reboot.isPending
              ? 'running'
              : rebootError
                ? 'failed'
                : 'idle'
          }
          statusLabel={rebooting ? 'Rebooting...' : 'Not run'}
          onRun={async () => {
            setRebootError(null)
            try {
              await reboot.mutateAsync({ serverId: server.id })
              rebootDeadline.current = null
              setRebooting(true)
              toast.info('Reboot initiated.')
            } catch (err) {
              const { message, output } = extractHardeningError(err)
              setRebootError(output ?? message)
              toast.error(message)
            }
          }}
          runLabel="Reboot"
          isRetry={Boolean(rebootError)}
          output={rebootError}
          // The reboot button stays usable during reboot only when it has failed; the
          // anyRunning lock is not applied here because reboot drives its own state.
          disabled={anyRunning && !reboot.isPending ? true : rebooting}
          disabledReason={
            rebooting ? 'Reboot already in progress.' : undefined
          }
        />
      </div>
    </div>
  )
}

// Projects cloned onto this server. The New Project dialog opens with this
// server preselected and the server field locked.
function ProjectsSection({ server }: { server: Server }) {
  const openNewProject = useNewProjectStore((s) => s.open)
  const projects = useProjectsByServer(server.id)

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Projects</h2>
        {projects.data && projects.data.length > 0 && (
          <Button
            size="sm"
            onClick={() => openNewProject({ initialServerId: server.id })}
          >
            New Project
          </Button>
        )}
      </div>

      {projects.isLoading && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="size-4 animate-spin" />
          Loading projects...
        </div>
      )}
      {projects.isError && (
        <p className="text-destructive text-sm">Could not load projects.</p>
      )}

      {projects.data && projects.data.length === 0 && (
        <div className="rounded-lg border border-dashed py-8 text-center">
          <p className="text-muted-foreground mb-3 text-sm">
            No projects on this server yet.
          </p>
          <Button
            size="sm"
            onClick={() => openNewProject({ initialServerId: server.id })}
          >
            New Project
          </Button>
        </div>
      )}

      {projects.data && projects.data.length > 0 && (
        <div className="flex flex-col gap-3">
          {projects.data.map((project) => (
            <ProjectCard key={project.id} project={project} />
          ))}
        </div>
      )}

      <NewProjectDialog />
    </div>
  )
}

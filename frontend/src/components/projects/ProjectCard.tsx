// A single project card, from top to bottom: header (name, repo link,
// settings gear), status row (runtime + traffic badges), actions row
// (Start/Restart, Publish, refresh), the environment section, and a
// collapsible details block (clone path, fingerprint, dates). No delete in
// v1; deletion arrives with the server-deletion milestone.

import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Check,
  ChevronDown,
  Copy,
  ExternalLink,
  Globe,
  Settings,
} from 'lucide-react'

import { type Project } from '@/api/projects'
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
import { EnvFilesSection } from '@/components/projects/EnvFilesSection'
import { ProjectSettingsDialog } from '@/components/projects/ProjectSettingsDialog'
import { PublishDialog } from '@/components/projects/PublishDialog'
import {
  RuntimeControls,
  RuntimeStatusBadge,
} from '@/components/projects/RuntimeControls'
import { cn } from '@/lib/utils'

function TrafficBadge({ project }: { project: Project }) {
  if (project.domain) {
    return (
      <Badge className="bg-blue-600 text-white">
        <Globe className="mr-1 size-3" />
        <a
          href={`https://${project.domain}`}
          target="_blank"
          rel="noreferrer"
          className="hover:underline"
        >
          {project.domain}
        </a>
      </Badge>
    )
  }
  return <Badge variant="outline">Unpublished</Badge>
}

export function ProjectCard({
  project,
  serverHost,
}: {
  project: Project
  // Server detail page passes the host directly; the global list embeds it.
  serverHost?: string
}) {
  const [showDetails, setShowDetails] = useState(false)
  const [showFullFingerprint, setShowFullFingerprint] = useState(false)
  const [copied, setCopied] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const host = serverHost ?? project.server_host
  const isRunning = project.runtime_status === 'running'

  const fingerprint = project.deploy_key_fingerprint
  const fingerprintDisplay =
    showFullFingerprint || fingerprint.length <= 24
      ? fingerprint
      : `${fingerprint.slice(0, 24)}...`

  const copyFingerprint = async () => {
    await navigator.clipboard.writeText(fingerprint)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{project.name}</CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <a
            href={`https://github.com/${project.github_repo_full_name}`}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 hover:underline"
          >
            {project.github_repo_full_name}
            <ExternalLink className="size-3" />
          </a>
          {project.server_name && (
            <span>
              on{' '}
              <Link
                to={`/servers/${project.server_id}`}
                className="hover:underline"
              >
                {project.server_name}
              </Link>
            </span>
          )}
        </CardDescription>
        <CardAction>
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            className="text-muted-foreground hover:text-foreground"
            title="Project settings"
          >
            <Settings className="size-4" />
          </button>
        </CardAction>
      </CardHeader>

      <CardContent className="flex flex-col gap-4">
        {/* Status row */}
        <div className="flex flex-wrap items-center gap-2">
          <RuntimeStatusBadge project={project} />
          <TrafficBadge project={project} />
        </div>

        {/* Actions row */}
        <div className="flex flex-wrap items-center gap-2">
          <RuntimeControls project={project} />
          <div title={isRunning ? undefined : 'Start your app before publishing'}>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setPublishOpen(true)}
              disabled={!isRunning || Boolean(project.domain)}
            >
              <Globe className="mr-1.5 size-3.5" />
              Publish
            </Button>
          </div>
        </div>

        <EnvFilesSection projectId={project.id} />

        {/* Details, collapsed by default */}
        <div>
          <button
            type="button"
            onClick={() => setShowDetails((v) => !v)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ChevronDown
              className={cn(
                'size-3 transition-transform',
                !showDetails && '-rotate-90',
              )}
            />
            Details
          </button>
          {showDetails && (
            <div className="mt-2 flex flex-col gap-2">
              <p className="font-mono text-xs text-muted-foreground">
                {project.clone_path}
              </p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setShowFullFingerprint((v) => !v)}
                  className="text-left font-mono text-xs text-muted-foreground hover:text-foreground"
                  title="Click to expand or collapse"
                >
                  {fingerprintDisplay}
                </button>
                <button
                  type="button"
                  onClick={copyFingerprint}
                  className="text-muted-foreground hover:text-foreground"
                  title="Copy fingerprint"
                >
                  {copied ? (
                    <Check className="size-3.5" />
                  ) : (
                    <Copy className="size-3.5" />
                  )}
                </button>
              </div>
              <p className="text-xs text-muted-foreground">
                {project.cloned_at &&
                  `Cloned ${new Date(project.cloned_at).toLocaleString()}. `}
                {project.started_at &&
                  `Last started ${new Date(project.started_at).toLocaleString()}. `}
                Created {new Date(project.created_at).toLocaleDateString()}.
              </p>
            </div>
          )}
        </div>
      </CardContent>

      <PublishDialog
        project={project}
        serverHost={host}
        open={publishOpen}
        onOpenChange={setPublishOpen}
      />
      <ProjectSettingsDialog
        project={project}
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
      />
    </Card>
  )
}

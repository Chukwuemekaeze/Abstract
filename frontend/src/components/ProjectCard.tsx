// A single project row. Shows the repo it tracks, where it lives (server and
// clone path), and the per-project deploy key fingerprint (expandable, with a
// copy button). No delete in v1; deletion arrives with the server-deletion
// milestone.

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, Copy, ExternalLink } from 'lucide-react'

import { type Project } from '@/api/projects'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

export function ProjectCard({ project }: { project: Project }) {
  const [showFullFingerprint, setShowFullFingerprint] = useState(false)
  const [copied, setCopied] = useState(false)

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
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p className="font-mono text-xs text-muted-foreground">
          {project.clone_path}
        </p>

        {/* Deploy key fingerprint: click to expand, button to copy. */}
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
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          </button>
        </div>

        <p className="text-muted-foreground text-xs">
          Created {new Date(project.created_at).toLocaleDateString()}
        </p>
      </CardContent>
    </Card>
  )
}

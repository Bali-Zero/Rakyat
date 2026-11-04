"use client";

import React, { useEffect, useState } from "react";
import {
  ArrowLeft,
  ExternalLink,
  FileText,
  Folder,
  Loader2,
} from "lucide-react";
import { api } from "@/lib/api";
import type { FileItem } from "@/lib/api/drive/drive.types";

// Reads the client's own Google Drive folder inside kita. Listing goes through
// GET /api/drive/files?folder_id= (team_drive.py, folder_access_rules apply);
// opening a file goes through GET /api/documents/proxy/{id}
// (documents_proxy.py), which only serves files whose folder ancestry is a
// known client folder — so a file dropped into the folder by hand or by an
// automation is readable here without leaving kita.

type Crumb = { id: string; name: string };

export function driveProxyHref(fileId: string): string {
  return `/api/documents/proxy/${encodeURIComponent(fileId)}`;
}

function formatModified(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

export function FilesBrowserPanel({ folderId }: { folderId: string }) {
  const [trail, setTrail] = useState<Crumb[]>([
    { id: folderId, name: "Client folder" },
  ]);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const current = trail[trail.length - 1];

  useEffect(() => {
    setTrail([{ id: folderId, name: "Client folder" }]);
  }, [folderId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.drive
      .listFiles({ folder_id: current.id, page_size: 100 })
      .then((res) => {
        if (cancelled) return;
        const sorted = [...res.files].sort((a, b) =>
          a.is_folder === b.is_folder
            ? a.name.localeCompare(b.name)
            : a.is_folder
              ? -1
              : 1,
        );
        setFiles(sorted);
      })
      .catch(() => {
        if (cancelled) return;
        setFiles([]);
        setError("Could not load the Drive folder.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [current.id]);

  return (
    <section
      data-testid="files-browser-panel"
      aria-label="Drive files"
      className="mt-6 border-t border-[var(--bz-border)] pt-4"
    >
      <div className="flex min-h-11 items-center gap-2">
        {trail.length > 1 && (
          <button
            type="button"
            onClick={() => setTrail((t) => t.slice(0, -1))}
            className="inline-flex items-center gap-1 text-sm text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
            aria-label="Back to parent folder"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
        <h2 className="text-base font-semibold text-[var(--tx-pure)]">
          Drive files
          {trail.length > 1 && (
            <span className="font-normal text-[var(--tx-secondary)]">
              {" "}
              / {current.name}
            </span>
          )}
        </h2>
        <a
          href={`https://drive.google.com/drive/folders/${encodeURIComponent(current.id)}`}
          target="_blank"
          rel="noopener noreferrer"
          className="ml-auto inline-flex items-center gap-1 text-xs text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
        >
          Open in Drive
          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
        </a>
      </div>

      {loading ? (
        <p className="flex items-center gap-2 py-3 text-sm text-[var(--tx-secondary)]">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Loading Drive folder…
        </p>
      ) : error ? (
        <p role="alert" className="py-3 text-sm text-[var(--state-warning)]">
          {error}
        </p>
      ) : files.length === 0 ? (
        <p className="py-3 text-sm text-[var(--tx-secondary)]">
          This folder is empty.
        </p>
      ) : (
        <ul className="divide-y divide-[var(--bz-border)]">
          {files.map((f) => (
            <li key={f.id} className="flex items-center gap-3 py-2 text-sm">
              {f.is_folder ? (
                <button
                  type="button"
                  onClick={() =>
                    setTrail((t) => [...t, { id: f.id, name: f.name }])
                  }
                  className="inline-flex min-w-0 items-center gap-2 text-left text-[var(--tx-pure)] hover:underline"
                >
                  <Folder className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{f.name}</span>
                </button>
              ) : (
                <a
                  href={driveProxyHref(f.id)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex min-w-0 items-center gap-2 text-[var(--tx-pure)] hover:underline"
                >
                  <FileText className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{f.name}</span>
                </a>
              )}
              <span className="ml-auto shrink-0 tabular-nums text-xs text-[var(--tx-secondary)]">
                {formatModified(f.modified_time)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

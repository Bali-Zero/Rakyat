"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import {
  ArrowLeft,
  ExternalLink,
  FileText,
  Folder,
  Loader2,
  Pencil,
  Trash2,
  Upload,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useInViewOnce } from "@/hooks/useInViewOnce";
import type { FileItem } from "@/lib/api/drive/drive.types";
import { FOCUS } from "@/components/workspace/r19";
import { Button } from "@/components/ui/button";
import {
  TAX_DOCUMENT_MAX_BYTES,
  TAX_DOCUMENT_MAX_LABEL,
} from "./tax-documents-access";

// Reads the client's own Google Drive folder inside kita. Listing goes through
// GET /api/drive/files?folder_id= (team_drive.py, folder_access_rules apply);
// opening a file goes through GET /api/documents/proxy/{id}
// (documents_proxy.py), which only serves files whose folder ancestry is a
// known client folder — so a file dropped into the folder by hand or by an
// automation is readable here without leaving kita.

type Crumb = { id: string; name: string };

export interface FilesBrowserActions {
  /** Maximum upload byte size enforced client-side before sending. */
  maxUploadBytes?: number;
  /** Human label for `maxUploadBytes`, shown in validation errors. */
  maxUploadLabel?: string;
  onUpload: (folderId: string, file: File) => Promise<unknown>;
  onRename: (file: FileItem, newName: string) => Promise<unknown>;
  onReplace: (
    file: FileItem,
    replacement: File,
    newName?: string,
  ) => Promise<unknown>;
  onDelete: (file: FileItem) => Promise<unknown>;
}

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

function openNativeDialog(el: HTMLDialogElement | null) {
  if (!el || el.open) return;
  if (typeof el.showModal === "function") {
    el.showModal();
  } else {
    el.setAttribute("open", "");
  }
}

function closeNativeDialog(el: HTMLDialogElement | null) {
  if (!el) return;
  if (typeof el.close === "function" && el.open) {
    el.close();
  } else {
    el.removeAttribute("open");
  }
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function FilesBrowserPanel({
  folderId,
  title = "Drive files",
  rootName = "Client folder",
  actions,
}: {
  folderId: string;
  /** Panel heading; the Documents tab keeps the default. */
  title?: string;
  /** Name of the starting folder in the breadcrumb trail. */
  rootName?: string;
  /** Optional Drive document write actions (offered only to authorized team members). */
  actions?: FilesBrowserActions;
}) {
  const [trail, setTrail] = useState<Crumb[]>([
    { id: folderId, name: rootName },
  ]);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);

  const [uploading, setUploading] = useState(false);
  const [editingFile, setEditingFile] = useState<FileItem | null>(null);
  const [editName, setEditName] = useState("");
  const [editNameTouched, setEditNameTouched] = useState(false);
  const [replacementFile, setReplacementFile] = useState<File | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);

  const [deletingFile, setDeletingFile] = useState<FileItem | null>(null);
  const [deleting, setDeleting] = useState(false);

  const uploadInputRef = useRef<HTMLInputElement>(null);
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const editDialogRef = useRef<HTMLDialogElement>(null);
  const deleteDialogRef = useRef<HTMLDialogElement>(null);

  const idPrefix = useId();
  const editTitleId = `${idPrefix}-edit-title`;
  const editNameId = `${idPrefix}-edit-name`;
  const editReplaceId = `${idPrefix}-edit-replace`;
  const deleteTitleId = `${idPrefix}-delete-title`;
  const deleteDescId = `${idPrefix}-delete-desc`;

  const maxUploadBytes = actions?.maxUploadBytes ?? TAX_DOCUMENT_MAX_BYTES;
  const maxUploadLabel = actions?.maxUploadLabel ?? TAX_DOCUMENT_MAX_LABEL;

  const current = trail[trail.length - 1];
  // The panel sits below the documents list: list the Drive folder only once
  // the panel nears the viewport, not on tab open.
  const sectionRef = useRef<HTMLElement>(null);
  const inView = useInViewOnce(sectionRef);

  useEffect(() => {
    setTrail([{ id: folderId, name: rootName }]);
  }, [folderId, rootName]);

  useEffect(() => {
    if (!inView) return;
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
  }, [current.id, inView, refreshNonce]);

  useEffect(() => {
    if (editingFile) {
      openNativeDialog(editDialogRef.current);
    } else {
      closeNativeDialog(editDialogRef.current);
    }
  }, [editingFile]);

  useEffect(() => {
    if (deletingFile) {
      openNativeDialog(deleteDialogRef.current);
    } else {
      closeNativeDialog(deleteDialogRef.current);
    }
  }, [deletingFile]);

  const validateFileSize = (file: File): boolean => {
    if (file.size === 0) {
      toast.error("File is empty");
      return false;
    }
    if (file.size > maxUploadBytes) {
      toast.error("File too large", {
        description: `Maximum upload size is ${maxUploadLabel}.`,
      });
      return false;
    }
    return true;
  };

  const handleUploadPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !actions) return;
    if (!validateFileSize(file)) return;

    setUploading(true);
    try {
      await actions.onUpload(current.id, file);
      toast.success(`Uploaded "${file.name}"`);
      setRefreshNonce((n) => n + 1);
    } catch (err) {
      toast.error("Upload failed", {
        description: errorMessage(err, "Could not upload document."),
      });
    } finally {
      setUploading(false);
    }
  };

  const openEdit = (file: FileItem) => {
    setEditingFile(file);
    setEditName(file.name);
    setEditNameTouched(false);
    setReplacementFile(null);
    if (replaceInputRef.current) replaceInputRef.current.value = "";
  };

  const closeEdit = () => {
    if (savingEdit) return;
    setEditingFile(null);
    setReplacementFile(null);
    if (replaceInputRef.current) replaceInputRef.current.value = "";
  };

  const handleReplacePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0] ?? null;
    if (!picked) {
      setReplacementFile(null);
      return;
    }
    if (!validateFileSize(picked)) {
      e.target.value = "";
      setReplacementFile(null);
      return;
    }
    setReplacementFile(picked);
    if (!editNameTouched) {
      setEditName(picked.name);
    }
  };

  const handleEditSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!actions || !editingFile || savingEdit) return;
    const trimmed = editName.trim();
    if (!trimmed) {
      toast.error("File name is required");
      return;
    }
    const nameChanged = trimmed !== editingFile.name;
    if (!nameChanged && !replacementFile) {
      closeEdit();
      return;
    }

    setSavingEdit(true);
    try {
      if (replacementFile) {
        await actions.onReplace(
          editingFile,
          replacementFile,
          nameChanged ? trimmed : undefined,
        );
        toast.success(`Updated "${trimmed}"`);
      } else {
        await actions.onRename(editingFile, trimmed);
        toast.success(`Renamed to "${trimmed}"`);
      }
      setEditingFile(null);
      setReplacementFile(null);
      if (replaceInputRef.current) replaceInputRef.current.value = "";
      setRefreshNonce((n) => n + 1);
    } catch (err) {
      toast.error("Could not update document", {
        description: errorMessage(err, "Update failed."),
      });
    } finally {
      setSavingEdit(false);
    }
  };

  const handleConfirmDelete = async () => {
    if (!actions || !deletingFile || deleting) return;
    const target = deletingFile;
    setDeleting(true);
    try {
      await actions.onDelete(target);
      toast.success(`Moved "${target.name}" to Drive Trash`);
      setDeletingFile(null);
      setRefreshNonce((n) => n + 1);
    } catch (err) {
      toast.error("Could not delete document", {
        description: errorMessage(err, "Delete failed."),
      });
    } finally {
      setDeleting(false);
    }
  };

  const canSaveEdit =
    !!editingFile &&
    editName.trim().length > 0 &&
    (editName.trim() !== editingFile.name || replacementFile !== null);

  return (
    <section
      ref={sectionRef}
      data-testid="files-browser-panel"
      aria-label={title}
      className="mt-6 border-t border-[var(--bz-border)] pt-4"
    >
      <div className="flex min-h-11 flex-wrap items-center gap-2">
        {trail.length > 1 && (
          <button
            type="button"
            onClick={() => setTrail((t) => t.slice(0, -1))}
            className={cn(
              "inline-flex items-center gap-1 text-sm text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]",
              FOCUS,
            )}
            aria-label="Back to parent folder"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
        <h2 className="text-base font-semibold text-[var(--tx-pure)]">
          {title}
          {trail.length > 1 && (
            <span className="font-normal text-[var(--tx-secondary)]">
              {" "}
              / {current.name}
            </span>
          )}
        </h2>
        <div className="ml-auto flex items-center gap-3">
          {actions && (
            <>
              <input
                ref={uploadInputRef}
                type="file"
                className="sr-only"
                tabIndex={-1}
                aria-label={`Upload document to ${current.name}`}
                onChange={handleUploadPick}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={uploading}
                onClick={() => uploadInputRef.current?.click()}
                className="h-8 gap-1.5 px-2.5 text-xs"
              >
                {uploading ? (
                  <Loader2
                    className="h-3.5 w-3.5 animate-spin"
                    aria-hidden="true"
                  />
                ) : (
                  <Upload className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                {uploading ? "Uploading…" : "Upload"}
              </Button>
            </>
          )}
          <a
            href={`https://drive.google.com/drive/folders/${encodeURIComponent(current.id)}`}
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              "inline-flex items-center gap-1 text-xs text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]",
              FOCUS,
            )}
          >
            Open in Drive
            <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        </div>
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
                  className={cn(
                    "inline-flex min-w-0 items-center gap-2 text-left text-[var(--tx-pure)] hover:underline",
                    FOCUS,
                  )}
                >
                  <Folder className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{f.name}</span>
                </button>
              ) : (
                <a
                  href={driveProxyHref(f.id)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={cn(
                    "inline-flex min-w-0 items-center gap-2 text-[var(--tx-pure)] hover:underline",
                    FOCUS,
                  )}
                >
                  <FileText className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{f.name}</span>
                </a>
              )}
              <span className="ml-auto shrink-0 tabular-nums text-xs text-[var(--tx-secondary)]">
                {formatModified(f.modified_time)}
              </span>
              {/* Row controls are always rendered (never hover-gated, so touch
                  devices can reach them). Only files, never folders. */}
              {actions && !f.is_folder && (
                <span className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    aria-label={`Edit ${f.name}`}
                    title="Edit document"
                    onClick={() => openEdit(f)}
                    className={cn(
                      "inline-flex h-7 w-7 items-center justify-center border border-transparent text-[var(--tx-secondary)] hover:border-[var(--line-control)] hover:text-[var(--tx-pure)]",
                      FOCUS,
                    )}
                  >
                    <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete ${f.name}`}
                    title="Delete document"
                    onClick={() => setDeletingFile(f)}
                    className={cn(
                      "inline-flex h-7 w-7 items-center justify-center border border-transparent text-[var(--tx-secondary)] hover:border-[var(--line-control)] hover:text-[var(--state-warning)]",
                      FOCUS,
                    )}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      {actions && (
        <>
          {/* Native modal <dialog> for rename / in-place file replacement. */}
          <dialog
            ref={editDialogRef}
            aria-labelledby={editTitleId}
            onCancel={(e) => {
              if (savingEdit) {
                e.preventDefault();
                return;
              }
              setEditingFile(null);
              setReplacementFile(null);
            }}
            onClose={() => {
              if (!savingEdit) {
                setEditingFile(null);
                setReplacementFile(null);
              }
            }}
            className="m-auto w-full max-w-md rounded-xl border border-[var(--bz-border)] bg-[var(--bz-base)] p-0 text-[var(--tx-pure)] shadow-2xl backdrop:bg-black/50"
          >
            {editingFile && (
              <form onSubmit={handleEditSubmit} className="space-y-4 p-5">
                <div>
                  <h3
                    id={editTitleId}
                    className="text-base font-semibold text-[var(--tx-pure)]"
                  >
                    Edit document
                  </h3>
                  <p className="mt-1 text-xs text-[var(--tx-secondary)]">
                    Rename this file or upload a replacement version (max{" "}
                    {maxUploadLabel}).
                  </p>
                </div>

                <div className="space-y-1.5">
                  <label
                    htmlFor={editNameId}
                    className="block text-xs font-medium text-[var(--tx-secondary)]"
                  >
                    File name
                  </label>
                  <input
                    id={editNameId}
                    type="text"
                    required
                    autoFocus
                    disabled={savingEdit}
                    value={editName}
                    onChange={(e) => {
                      setEditName(e.target.value);
                      setEditNameTouched(true);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        if (e.nativeEvent.isComposing || e.keyCode === 229) {
                          return;
                        }
                        if (
                          typeof e.currentTarget.form?.requestSubmit ===
                          "function"
                        ) {
                          e.currentTarget.form.requestSubmit();
                        } else {
                          e.currentTarget.form?.dispatchEvent(
                            new Event("submit", {
                              bubbles: true,
                              cancelable: true,
                            }),
                          );
                        }
                      }
                    }}
                    className={cn(
                      "w-full rounded border border-[var(--line-control)] bg-transparent px-3 py-2 text-sm text-[var(--tx-pure)]",
                      FOCUS,
                    )}
                  />
                </div>

                <div className="space-y-1.5">
                  <label
                    htmlFor={editReplaceId}
                    className="block text-xs font-medium text-[var(--tx-secondary)]"
                  >
                    Replace file (optional)
                  </label>
                  <input
                    ref={replaceInputRef}
                    id={editReplaceId}
                    type="file"
                    disabled={savingEdit}
                    onChange={handleReplacePick}
                    className={cn(
                      "block w-full text-xs text-[var(--tx-secondary)] file:mr-3 file:rounded file:border file:border-[var(--line-control)] file:bg-transparent file:px-2.5 file:py-1.5 file:text-xs file:text-[var(--tx-pure)]",
                      FOCUS,
                    )}
                  />
                  {replacementFile && (
                    <p className="text-xs text-[var(--tx-secondary)]">
                      Selected: {replacementFile.name} (
                      {Math.max(1, Math.round(replacementFile.size / 1024))} KB)
                    </p>
                  )}
                </div>

                <div className="flex items-center justify-end gap-2 pt-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={savingEdit}
                    onClick={closeEdit}
                  >
                    Cancel
                  </Button>
                  <Button
                    type="submit"
                    variant="outline"
                    size="sm"
                    disabled={savingEdit || !canSaveEdit}
                  >
                    {savingEdit ? "Saving…" : "Save changes"}
                  </Button>
                </div>
              </form>
            )}
          </dialog>

          {/* Native modal <dialog> for destructive confirmation; Cancel gets autofocus. */}
          <dialog
            ref={deleteDialogRef}
            aria-labelledby={deleteTitleId}
            aria-describedby={deleteDescId}
            onCancel={(e) => {
              if (deleting) {
                e.preventDefault();
                return;
              }
              setDeletingFile(null);
            }}
            onClose={() => {
              if (!deleting) setDeletingFile(null);
            }}
            className="m-auto w-full max-w-md rounded-xl border border-[var(--bz-border)] bg-[var(--bz-base)] p-0 text-[var(--tx-pure)] shadow-2xl backdrop:bg-black/50"
          >
            {deletingFile && (
              <div className="space-y-4 p-5">
                <div>
                  <h3
                    id={deleteTitleId}
                    className="text-base font-semibold text-[var(--tx-pure)]"
                  >
                    Delete document?
                  </h3>
                  <p
                    id={deleteDescId}
                    className="mt-1.5 text-sm text-[var(--tx-secondary)]"
                  >
                    &ldquo;{deletingFile.name}&rdquo; will be moved to Google
                    Drive Trash and can be restored from Drive if needed.
                  </p>
                </div>
                <div className="flex items-center justify-end gap-2 pt-1">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    autoFocus
                    disabled={deleting}
                    onClick={() => setDeletingFile(null)}
                  >
                    Cancel
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={deleting}
                    onClick={handleConfirmDelete}
                    className="border-[var(--state-warning)] text-[var(--state-warning)]"
                  >
                    {deleting ? "Deleting…" : "Delete"}
                  </Button>
                </div>
              </div>
            )}
          </dialog>
        </>
      )}
    </section>
  );
}

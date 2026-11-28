"use client";

import { useState, memo } from "react";
import {
  FileText,
  Plus,
  Trash2,
  CheckCircle,
  Upload,
  AlertCircle,
  FileCheck,
  X,
  ChevronDown,
  Eye,
  Pencil,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "sonner";
import { ApiError } from "@/lib/api/error-handler";
import { useRequiredDocuments } from "@/lib/hooks/useRequiredDocuments";
import { nameOf, shortDate } from "@/lib/document-request-format";
import {
  RequiredDocument,
  DocumentRequestCreate,
  DocumentRequestState,
  DocumentStatus,
  PracticeDocumentRequest,
  DOCUMENT_TYPE_OPTIONS,
} from "@/lib/types/required-documents";

interface RequiredDocumentsCardProps {
  readOnly?: boolean;
  practiceId: number;
}

// Status Badge Component
const StatusBadge = memo(({ status }: { status: DocumentStatus }) => {
  const configs: Record<
    DocumentStatus,
    { color: string; icon: React.ReactNode; label: string }
  > = {
    pending: {
      color:
        "bg-[color-mix(in_srgb,var(--state-warning)_10%,transparent)] text-[var(--state-warning)]",
      icon: <AlertCircle className="w-3 h-3" />,
      label: "Pending",
    },
    uploaded: {
      color:
        "bg-[color-mix(in_srgb,var(--state-info)_10%,transparent)] text-[var(--state-info)]",
      icon: <Upload className="w-3 h-3" />,
      label: "Uploaded",
    },
    verified: {
      color:
        "bg-[color-mix(in_srgb,var(--state-success)_10%,transparent)] text-[var(--state-success)]",
      icon: <CheckCircle className="w-3 h-3" />,
      label: "Verified",
    },
    rejected: {
      color:
        "bg-[color-mix(in_srgb,var(--state-danger)_10%,transparent)] text-[var(--state-danger)]",
      icon: <X className="w-3 h-3" />,
      label: "Rejected",
    },
  };
  const config = configs[status];
  return (
    <Badge
      variant="secondary"
      className={`${config.color} flex items-center gap-1`}
    >
      {config.icon}
      {config.label}
    </Badge>
  );
});
StatusBadge.displayName = "StatusBadge";

// Ask for one more document
const SUGGESTIONS = DOCUMENT_TYPE_OPTIONS.filter((o) => o.value !== "other");

/** The message a failed request shows; a 409 is picked from `detail.code`. */
function requestErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) {
      const code = (err.data as { detail?: { code?: unknown } } | undefined)
        ?.detail?.code;
      if (code === "already_on_practice")
        return "This document is already on this practice.";
      if (code === "practice_closed")
        return "This practice is closed: no document can be asked on it.";
      return "This request conflicts with the practice as it is now.";
    }
    if (err.status === 403)
      return "You do not have permission to ask for documents on this practice.";
    if (err.status === 422)
      return "Check the fields: one of them is empty or too long.";
  }
  return "The request could not be sent. Please try again.";
}

function AddDocumentModal({
  isOpen,
  onClose,
  onRequest,
  isLoading,
}: {
  isOpen: boolean;
  onClose: () => void;
  onRequest: (data: DocumentRequestCreate) => Promise<unknown>;
  isLoading: boolean;
}) {
  const [name, setName] = useState("");
  const [clientText, setClientText] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const trimmed = {
    name: name.trim(),
    text: clientText.trim(),
    why: reason.trim(),
  };
  const ready = !!trimmed.name && !!trimmed.text && !!trimmed.why;

  const close = () => {
    setError(null);
    onClose();
  };

  const handleSubmit = async () => {
    if (!ready) return;
    const suggestion = SUGGESTIONS.find((o) => o.label === trimmed.name);
    setError(null);
    try {
      await onRequest({
        document_label: trimmed.name,
        client_text: trimmed.text,
        reason: trimmed.why,
        ...(suggestion ? { document_type: suggestion.value } : {}),
      });
    } catch (err) {
      setError(requestErrorMessage(err));
      return;
    }
    setName("");
    setClientText("");
    setReason("");
    close();
  };

  return (
    <Dialog open={isOpen} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Ask for a document</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <Label htmlFor="request-document-name">Document</Label>
            <Input
              id="request-document-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              list="request-document-suggestions"
              placeholder="Passport, proof of accommodation..."
              maxLength={100}
              required
            />
            <datalist id="request-document-suggestions">
              {SUGGESTIONS.map((option) => (
                <option key={option.value} value={option.label} />
              ))}
            </datalist>
          </div>

          <div>
            <Label htmlFor="request-client-text">What the client reads</Label>
            <Textarea
              id="request-client-text"
              value={clientText}
              onChange={(e) => setClientText(e.target.value)}
              placeholder="Tell the client what to send..."
              rows={3}
              maxLength={2000}
              required
            />
          </div>

          <div>
            <Label htmlFor="request-reason">Why this case needs it</Label>
            <Textarea
              id="request-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={2}
              maxLength={1000}
              required
            />
            <p className="text-xs text-muted-foreground mt-1">
              Do not write the client&apos;s name here.
            </p>
          </div>

          {error && (
            <p role="alert" className="text-sm text-[var(--state-danger)]">
              {error}
            </p>
          )}

          <div className="flex gap-2 justify-end">
            <Button variant="outline" onClick={close} disabled={isLoading}>
              Cancel
            </Button>
            <Button onClick={handleSubmit} disabled={!ready || isLoading}>
              {isLoading ? "Sending..." : "Ask for document"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// Where a request stands, in the words the consultant reads
const REQUEST_STATE_TEXT: Record<DocumentRequestState, string> = {
  pending: "waiting for the card review",
  added_to_card: "now on the card",
  case_only: "this case only",
};

/** The slot states in which the text can still be corrected (the client has not answered). */
const CORRECTABLE_STATUSES: DocumentStatus[] = ["pending", "rejected"];

/** The message a failed correction shows; a 409 or a 404 is picked from `detail.code`. */
function correctionErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const code = (err.data as { detail?: { code?: unknown } } | undefined)
      ?.detail?.code;
    if (code === "practice_closed")
      return "This practice is closed: its documents can no longer be changed.";
    if (code === "slot_withdrawn")
      return "This document was removed from the practice.";
    if (code === "slot_answered")
      return "The client already sent this document: mark it rejected first.";
    if (code === "request_not_found") return "This request no longer exists.";
    if (err.status === 403)
      return "You cannot change documents on this practice.";
  }
  return "Could not save the text. Try again.";
}

function CorrectTextDialog({
  document,
  request,
  onClose,
  onCorrect,
}: {
  document: RequiredDocument;
  request: PracticeDocumentRequest;
  onClose: () => void;
  onCorrect: (requestId: number, clientText: string) => Promise<unknown>;
}) {
  const current = document.description ?? "";
  const [text, setText] = useState(current);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = text.trim();
  const canSave = !!trimmed && trimmed !== current.trim() && !sending;

  const save = async () => {
    if (!canSave) return;
    setSending(true);
    setError(null);
    try {
      await onCorrect(request.id, trimmed);
    } catch (err) {
      setError(correctionErrorMessage(err));
      setSending(false);
      return;
    }
    setSending(false);
    toast.success("Text corrected");
    onClose();
  };

  return (
    <Dialog open onOpenChange={(open) => !open && !sending && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Correct the text for the client</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="text-sm text-muted-foreground space-y-1">
            <p>
              The client sees the new text in the portal at once; nothing is
              sent to them. The reviewers are not emailed again.
            </p>
            {request.state === "added_to_card" && (
              <p>
                The service card keeps its own text: only this practice changes.
              </p>
            )}
          </div>
          <div>
            <Label htmlFor="correct-client-text">What the client reads</Label>
            <Textarea
              id="correct-client-text"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={4}
              maxLength={2000}
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-[var(--state-danger)]">
              {error}
            </p>
          )}
          <div className="flex gap-2 justify-end">
            <Button variant="outline" onClick={onClose} disabled={sending}>
              Cancel
            </Button>
            <Button onClick={() => void save()} disabled={!canSave}>
              Save text
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// Document Review Modal
function DocumentReviewModal({
  document,
  isOpen,
  onClose,
  onUpdate,
  isLoading,
}: {
  document: RequiredDocument | null;
  isOpen: boolean;
  onClose: () => void;
  onUpdate: (
    docId: number,
    data: { status: DocumentStatus; team_member_notes: string },
  ) => Promise<void>;
  isLoading: boolean;
}) {
  const [status, setStatus] = useState<DocumentStatus>("pending");
  const [notes, setNotes] = useState("");

  // Reset when document changes
  const handleOpenChange = (open: boolean) => {
    if (open && document) {
      setStatus(document.status);
      setNotes(document.team_member_notes || "");
    }
    if (!open) onClose();
  };

  const handleSubmit = async () => {
    if (!document) return;
    await onUpdate(document.id, { status, team_member_notes: notes });
    onClose();
  };

  if (!document) return null;

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Review Document</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="p-3 bg-muted rounded-lg">
            <p className="font-medium">{document.document_label}</p>
            <p className="text-sm text-muted-foreground">
              {document.document_type}
            </p>
            {document.client_notes && (
              <p className="text-sm mt-2">
                <strong>Client notes:</strong> {document.client_notes}
              </p>
            )}
            {!document.uploaded_by_client && (
              <p className="text-sm mt-2 text-muted-foreground">
                Not uploaded through this slot. Mark it Verified only after
                checking the file in the client&apos;s Documents.
              </p>
            )}
          </div>

          <div>
            <Label>Status</Label>
            <Select
              value={status}
              onValueChange={(v) => setStatus(v as DocumentStatus)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="pending">Pending</SelectItem>
                <SelectItem value="uploaded">Uploaded</SelectItem>
                <SelectItem value="verified">Verified</SelectItem>
                <SelectItem value="rejected">Rejected</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <Label>Team Notes</Label>
            <Textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Add notes for the team..."
              rows={3}
            />
          </div>

          <div className="flex gap-2 justify-end">
            <Button variant="outline" onClick={onClose} disabled={isLoading}>
              Cancel
            </Button>
            <Button onClick={handleSubmit} disabled={isLoading}>
              {isLoading ? "Saving..." : "Save Review"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// Main Component
export function RequiredDocumentsCard({
  practiceId,
  readOnly = false,
}: RequiredDocumentsCardProps) {
  const {
    documents,
    isLoading,
    stats,
    requestDocument,
    updateDocument,
    deleteDocument,
    requestsBySlot,
    correctRequestText: correctRequestTextOrAbsent,
  } = useRequiredDocuments({ practiceId });
  // A test may mock the hook with its base shape: absent means no states and no button.
  const requestStates = requestsBySlot ?? {};
  const correctRequestText = correctRequestTextOrAbsent as
    typeof correctRequestTextOrAbsent | undefined;

  const [showAddModal, setShowAddModal] = useState(false);
  const [reviewDoc, setReviewDoc] = useState<RequiredDocument | null>(null);
  const [correctDoc, setCorrectDoc] = useState<RequiredDocument | null>(null);
  const [processingId, setProcessingId] = useState<number | null>(null);

  const handleDelete = (docId: number) => {
    toast("Remove this document requirement?", {
      action: {
        label: "Remove",
        onClick: async () => {
          setProcessingId(docId);
          try {
            await deleteDocument(docId);
          } catch {
            toast.error("Failed to delete document");
          } finally {
            setProcessingId(null);
          }
        },
      },
      cancel: { label: "Cancel", onClick: () => toast.dismiss() },
    });
  };

  return (
    <div className="bg-card rounded-xl border shadow-sm">
      {/* Header */}
      <div className="p-4 border-b">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <FileCheck className="w-5 h-5 text-[var(--accent)]" />
            <h3 className="font-semibold">Required Documents</h3>
            <Badge variant="secondary" className="ml-2">
              {stats.verified}/{stats.total}
            </Badge>
          </div>
          {!readOnly && (
            <Button size="sm" onClick={() => setShowAddModal(true)}>
              <Plus className="w-4 h-4 mr-1" />
              Add
            </Button>
          )}
        </div>

        {/* Progress Bar */}
        {stats.total > 0 && (
          <div className="mt-3">
            <div className="flex justify-between text-xs text-muted-foreground mb-1">
              <span>Completion</span>
              <span>{stats.completionPercentage}%</span>
            </div>
            <div className="h-2 bg-muted rounded-full overflow-hidden">
              <div
                className="h-full bg-[var(--accent)] transition-all"
                style={{ width: `${stats.completionPercentage}%` }}
              />
            </div>
          </div>
        )}
      </div>

      {/* Document List */}
      <div className="divide-y">
        {isLoading ? (
          <div className="p-8 text-center text-muted-foreground">
            <div className="animate-spin w-6 h-6 border-2 border-[var(--accent)] border-t-transparent rounded-full mx-auto mb-2" />
            Loading documents...
          </div>
        ) : documents.length === 0 ? (
          <div className="p-8 text-center text-muted-foreground">
            <FileText className="w-12 h-12 mx-auto mb-3 opacity-30" />
            <p>No required documents set</p>
            {!readOnly && (
              <>
                <p className="text-sm">
                  Add documents that the client needs to provide
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-4"
                  onClick={() => setShowAddModal(true)}
                >
                  <Plus className="w-4 h-4 mr-1" />
                  Add First Document
                </Button>
              </>
            )}
          </div>
        ) : (
          documents.map((doc) => {
            const request = requestStates[doc.id];
            return (
              <div
                key={doc.id}
                className={`p-3 hover:bg-muted/50 transition-colors ${
                  doc.is_required
                    ? "border-l-2 border-l-[var(--state-warning)]"
                    : ""
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium truncate">
                        {doc.document_label}
                      </span>
                      {doc.is_required && (
                        <Badge
                          variant="secondary"
                          className="text-[10px] bg-[color-mix(in_srgb,var(--state-warning)_10%,transparent)] text-[var(--state-warning)]"
                        >
                          Required
                        </Badge>
                      )}
                    </div>
                    {doc.description && (
                      <p className="text-xs text-muted-foreground truncate">
                        {doc.description}
                      </p>
                    )}
                    <div className="flex items-center gap-2 mt-1">
                      <StatusBadge status={doc.status} />
                      {doc.uploaded_by_client && (
                        <span className="text-xs text-[var(--state-success)] flex items-center gap-1">
                          <CheckCircle className="w-3 h-3" />
                          Uploaded
                        </span>
                      )}
                    </div>
                    {request && (
                      <p className="text-xs text-muted-foreground mt-1">
                        {`Added by ${nameOf(request.requested_by)} · ${shortDate(
                          request.requested_at,
                        )} · ${REQUEST_STATE_TEXT[request.state] ?? ""}`}
                      </p>
                    )}
                  </div>

                  {!readOnly && (
                    <div className="flex items-center gap-1">
                      {request &&
                        correctRequestText &&
                        CORRECTABLE_STATUSES.includes(doc.status) && (
                          <Button
                            size="icon"
                            variant="ghost"
                            className="h-8 w-8"
                            onClick={() => setCorrectDoc(doc)}
                            aria-label="Correct the text for the client"
                            title="Correct the text for the client"
                          >
                            <Pencil className="w-4 h-4" />
                          </Button>
                        )}
                      {/* Every slot gets the status action: a document can reach
                      the team through the Vault, WhatsApp or email, not only
                      through this slot's upload button. */}
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8"
                        onClick={() => setReviewDoc(doc)}
                        aria-label={
                          doc.uploaded_by_client
                            ? "Review document"
                            : "Update status"
                        }
                        title={
                          doc.uploaded_by_client
                            ? "Review document"
                            : "Update status"
                        }
                      >
                        <Eye className="w-4 h-4" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8 text-destructive hover:text-destructive"
                        onClick={() => handleDelete(doc.id)}
                        disabled={processingId === doc.id}
                        aria-label="Delete document"
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Modals */}
      <AddDocumentModal
        isOpen={showAddModal}
        onClose={() => setShowAddModal(false)}
        onRequest={requestDocument}
        isLoading={isLoading}
      />

      {correctDoc && requestStates[correctDoc.id] && correctRequestText && (
        <CorrectTextDialog
          key={correctDoc.id}
          document={documents.find((d) => d.id === correctDoc.id) ?? correctDoc}
          request={requestStates[correctDoc.id]}
          onClose={() => setCorrectDoc(null)}
          onCorrect={correctRequestText}
        />
      )}

      <DocumentReviewModal
        document={reviewDoc}
        isOpen={!!reviewDoc}
        onClose={() => setReviewDoc(null)}
        onUpdate={updateDocument}
        isLoading={isLoading}
      />
    </div>
  );
}

"use client";

/**
 * Required Documents Hook
 *
 * Manages required documents for practice workflows with optimized caching
 * and TypeScript safety.
 *
 * @ai_onboarding - Strict TypeScript, useMemo for stats, proper error handling
 */

import { useState, useCallback, useEffect, useMemo } from "react";
import { api } from "@/lib/api";
import {
  RequiredDocument,
  RequiredDocumentCreate,
  RequiredDocumentUpdate,
  DocumentRequestCreate,
  DocumentRequestResult,
  DocumentRequestTextCorrection,
  PracticeDocumentRequest,
  ClientDocumentUpload,
  ClientRequiredDocument,
} from "@/lib/types/required-documents";

interface UseRequiredDocumentsOptions {
  practiceId?: number;
  clientId?: number;
}

export function useRequiredDocuments(
  options: UseRequiredDocumentsOptions = {},
) {
  const { practiceId, clientId } = options;
  const [documents, setDocuments] = useState<RequiredDocument[]>([]);
  const [clientDocuments, setClientDocuments] = useState<
    ClientRequiredDocument[]
  >([]);
  const [requests, setRequests] = useState<PracticeDocumentRequest[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Where each document request of the practice stands. Asked only after the documents
  // answered; a failure leaves the rows and the states as they were and says nothing.
  const fetchRequestStates = useCallback(async () => {
    if (!practiceId) return;
    try {
      const result = await api.get<PracticeDocumentRequest[]>(
        `/api/crm/practices/${practiceId}/document-requests`,
      );
      setRequests(Array.isArray(result) ? result : []);
    } catch {
      // swallowed on purpose: the documents of the practice must keep showing
    }
  }, [practiceId]);

  // Fetch required documents for a practice (CRM view)
  const fetchPracticeDocuments = useCallback(async () => {
    if (!practiceId) return;

    setIsLoading(true);
    setError(null);

    try {
      const result = await api.get<RequiredDocument[]>(
        `/api/crm/practices/${practiceId}/required-documents`,
      );
      setDocuments(result || []);
      void fetchRequestStates();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load documents");
    } finally {
      setIsLoading(false);
    }
  }, [practiceId, fetchRequestStates]);

  // Fetch client's required documents across all practices (Portal view)
  const fetchClientDocuments = useCallback(async () => {
    if (!clientId) return;

    setIsLoading(true);
    setError(null);

    try {
      const result = await api.get<ClientRequiredDocument[]>(
        `/api/crm/clients/client/${clientId}/required-documents`,
      );
      setClientDocuments(result || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load documents");
    } finally {
      setIsLoading(false);
    }
  }, [clientId]);

  // Add a required document (team member)
  const addDocument = useCallback(
    async (data: RequiredDocumentCreate) => {
      if (!practiceId) throw new Error("Practice ID required");

      setIsLoading(true);
      try {
        const created = await api.post<RequiredDocument>(
          `/api/crm/practices/${practiceId}/required-documents`,
          data,
        );
        setDocuments((current) => {
          const existingIndex = current.findIndex(
            (document) => document.id === created.id,
          );
          if (existingIndex === -1) {
            return [...current, created];
          }

          return current.map((document, index) =>
            index === existingIndex ? created : document,
          );
        });
      } catch (err) {
        throw err;
      } finally {
        setIsLoading(false);
      }
    },
    [practiceId],
  );

  // Ask the client for one more document, with the reason (team member)
  const requestDocument = useCallback(
    async (data: DocumentRequestCreate) => {
      if (!practiceId) throw new Error("Practice ID required");

      setIsLoading(true);
      try {
        const result = await api.post<DocumentRequestResult>(
          `/api/crm/practices/${practiceId}/document-requests`,
          data,
        );
        setDocuments((current) =>
          current.some((document) => document.id === result.document.id)
            ? current
            : [...current, result.document],
        );
        void fetchRequestStates();
        return result;
      } finally {
        setIsLoading(false);
      }
    },
    [practiceId, fetchRequestStates],
  );

  // Correct the text the client reads for a slot a request created (team member).
  const correctRequestText = useCallback(
    async (requestId: number, clientText: string) => {
      if (!practiceId) throw new Error("Practice ID required");

      const body: DocumentRequestTextCorrection = { client_text: clientText };
      const result = await api.patch<DocumentRequestResult>(
        `/api/crm/practices/${practiceId}/document-requests/${requestId}`,
        body,
      );
      setDocuments((current) =>
        current.map((document) =>
          document.id === result.document.id ? result.document : document,
        ),
      );
      return result;
    },
    [practiceId],
  );

  // Update a required document (team member review)
  const updateDocument = useCallback(
    async (docId: number, data: RequiredDocumentUpdate) => {
      if (!practiceId) throw new Error("Practice ID required");

      setIsLoading(true);
      try {
        const updated = await api.patch<RequiredDocument>(
          `/api/crm/practices/${practiceId}/required-documents/${docId}`,
          data,
        );
        setDocuments((current) =>
          current.map((document) =>
            document.id === updated.id ? updated : document,
          ),
        );
      } catch (err) {
        throw err;
      } finally {
        setIsLoading(false);
      }
    },
    [practiceId],
  );

  // Delete a required document
  const deleteDocument = useCallback(
    async (docId: number) => {
      if (!practiceId) throw new Error("Practice ID required");

      setIsLoading(true);
      try {
        await api.delete<void>(
          `/api/crm/practices/${practiceId}/required-documents/${docId}`,
        );
        setDocuments((current) =>
          current.filter((document) => document.id !== docId),
        );
      } catch (err) {
        throw err;
      } finally {
        setIsLoading(false);
      }
    },
    [practiceId],
  );

  // Client uploads a document
  const uploadClientDocument = useCallback(
    async (data: ClientDocumentUpload) => {
      if (!practiceId) throw new Error("Practice ID required");

      setIsLoading(true);
      try {
        await api.post<RequiredDocument>(
          `/api/crm/practices/${practiceId}/upload-client-document`,
          data,
        );
        await fetchPracticeDocuments();
      } catch (err) {
        throw err;
      } finally {
        setIsLoading(false);
      }
    },
    [practiceId, fetchPracticeDocuments],
  );

  // Auto-fetch on mount
  useEffect(() => {
    if (practiceId) {
      fetchPracticeDocuments();
    }
  }, [practiceId, fetchPracticeDocuments]);

  useEffect(() => {
    if (clientId) {
      fetchClientDocuments();
    }
  }, [clientId, fetchClientDocuments]);

  // The newest request of each slot (the list comes newest first; the id decides anyway)
  const requestsBySlot = useMemo(() => {
    const bySlot: Record<number, PracticeDocumentRequest> = {};
    for (const request of requests) {
      const slot = request.required_document_id;
      if (slot === null || slot === undefined) continue;
      const known = bySlot[slot];
      if (!known || request.id > known.id) bySlot[slot] = request;
    }
    return bySlot;
  }, [requests]);

  // Stats - optimized with useMemo
  const stats = useMemo(
    () => ({
      total: documents.length,
      required: documents.filter((d) => d.is_required).length,
      uploaded: documents.filter((d) => d.uploaded_by_client).length,
      verified: documents.filter((d) => d.status === "verified").length,
      pending: documents.filter((d) => d.status === "pending").length,
      completionPercentage:
        documents.length > 0
          ? Math.round(
              (documents.filter((d) => d.status === "verified").length /
                documents.length) *
                100,
            )
          : 0,
    }),
    [documents],
  );

  return {
    documents,
    clientDocuments,
    isLoading,
    error,
    stats,
    addDocument,
    requestDocument,
    requestsBySlot,
    correctRequestText,
    updateDocument,
    deleteDocument,
    uploadClientDocument,
    refetch: fetchPracticeDocuments,
    refetchClient: fetchClientDocuments,
  };
}

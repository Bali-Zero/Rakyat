"use client";

import useSWR from "swr";
import { api } from "@/lib/api";

interface Documents {
  documents: Array<{ id: number; file_name: string }>;
}

export function DeliveredDocuments({ practiceId }: { practiceId: string }) {
  const path = `/api/portal/process/${encodeURIComponent(practiceId)}/delivery-documents`;
  const { data, error, mutate } = useSWR<Documents>(
    path,
    (url: string) => api.get<Documents>(url),
    { shouldRetryOnError: false },
  );
  if (!error && !data?.documents?.length) return null;
  return (
    <section className="my-6" aria-label="Delivered documents">
      <h2 className="text-lg font-semibold mb-2">Delivered documents</h2>
      {error && (
        <p role="alert">
          Unable to load documents.{" "}
          <button onClick={() => void mutate()} className="underline">
            Retry
          </button>
        </p>
      )}
      <ul className="space-y-2">
        {data?.documents?.map((doc) => (
          <li key={doc.id}>
            <a
              href={`/api/portal/documents/${doc.id}/download`}
              download
              className="underline text-[var(--bz-copper-text)] break-words"
            >
              {doc.file_name}
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

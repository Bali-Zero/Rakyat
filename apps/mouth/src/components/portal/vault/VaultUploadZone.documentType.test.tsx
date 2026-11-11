import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// Real component + real useVaultUpload: only the network is faked, so the
// assertion reads the multipart body the backend would receive.
vi.mock("@/lib/api", () => ({
  api: {
    getUserProfile: () => ({ role: "client" }),
    getPortalImpersonation: () => null,
  },
}));

class FakeXHR {
  static last: FakeXHR | null = null;
  upload = { onprogress: null };
  onload = null;
  onerror = null;
  withCredentials = false;
  sentBody: FormData | null = null;
  constructor() {
    FakeXHR.last = this;
  }
  open() {}
  send(body: FormData) {
    this.sentBody = body;
  }
}

import { VaultUploadZone } from "./VaultUploadZone";

function chooseFile() {
  const input = screen.getByLabelText(
    "Choose file to upload",
  ) as HTMLInputElement;
  const file = new File(["x"], "scan.pdf", { type: "application/pdf" });
  Object.defineProperty(input, "files", { value: [file] });
  fireEvent.change(input);
}

describe("VaultUploadZone — the client says what the document is", () => {
  const originalXHR = globalThis.XMLHttpRequest;

  beforeEach(() => {
    FakeXHR.last = null;
    globalThis.XMLHttpRequest = FakeXHR as unknown as typeof XMLHttpRequest;
  });

  afterEach(() => {
    globalThis.XMLHttpRequest = originalXHR;
  });

  // GUILT: the Vault always sent document_type "other", which the backend's
  // Required Documents matcher treats as untyped — the passport slot never
  // filled from a Vault upload.
  it("sends the chosen type (passport) as document_type", () => {
    render(<VaultUploadZone practiceId={7} />);
    fireEvent.change(screen.getByLabelText("What is this document?"), {
      target: { value: "passport" },
    });
    chooseFile();

    expect(FakeXHR.last?.sentBody?.get("document_type")).toBe("passport");
    expect(FakeXHR.last?.sentBody?.get("practice_id")).toBe("7");
  });

  it('keeps the old behaviour when nothing is chosen: document_type "other"', () => {
    render(<VaultUploadZone practiceId={7} />);
    chooseFile();

    expect(FakeXHR.last?.sentBody?.get("document_type")).toBe("other");
  });
});

/**
 * A photo the server refuses is sent again as the picture alone.
 *
 * The server takes one picture per JPEG with nothing after it, and answers 415
 * to anything else. A phone camera can write a second picture or a trailer
 * after the first. The browser can read such a photo, so the upload sends it
 * once more as the picture alone. Invented files only.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { MAX_SIZE_BYTES } from "@/lib/vault/uploadLimits";
import { useVaultUpload, type UploadState } from "./useVaultUpload";

const browser = vi.hoisted(() => ({ shrinkImage: vi.fn() }));
vi.mock("@/lib/vault/shrinkImage", () => ({
  shrinkImage: browser.shrinkImage,
}));
vi.mock("@/lib/api", () => ({
  api: {
    getUserProfile: () => ({ role: "client" }),
    getPortalImpersonation: () => null,
  },
}));

class FakeRequest {
  static all: FakeRequest[] = [];
  upload = { onprogress: null as ((e: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  status = 0;
  responseText = "";
  withCredentials = false;
  opened: { method: string; url: string } | null = null;
  body: FormData | null = null;

  constructor() {
    FakeRequest.all.push(this);
  }
  open(method: string, url: string) {
    this.opened = { method, url };
  }
  send(body: FormData) {
    this.body = body;
  }
  answer(status: number, responseText: string) {
    this.status = status;
    this.responseText = responseText;
    this.onload?.();
  }
}

const REFUSAL = "File content does not match its extension";
const ACCEPTED = JSON.stringify({
  success: true,
  message: "Uploaded",
  data: {
    id: 99,
    type: "passport",
    name: "IMG_0001.jpg",
    status: "received",
    size_kb: 1,
    created_at: "2026-04-18T00:00:00Z",
    expiry_date: null,
  },
});
const WORD =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

function fileOf(name: string, type: string, size = 2048): File {
  return new File([new Uint8Array(size)], name, { type });
}
const aPhoto = () => fileOf("IMG_0001.jpeg", "image/jpeg");
const thePictureAlone = () => fileOf("IMG_0001.jpg", "image/jpeg", 512);

function refuse(request: FakeRequest, status = 415) {
  act(() => request.answer(status, JSON.stringify({ detail: REFUSAL })));
}

function mount() {
  const seen: UploadState["status"][] = [];
  const view = renderHook(() => {
    const hook = useVaultUpload();
    seen.push(hook.state.status);
    return hook;
  });
  return { result: view.result, seen };
}

function sentFile(request: FakeRequest): File {
  return request.body!.get("file") as File;
}

beforeEach(() => {
  FakeRequest.all = [];
  browser.shrinkImage.mockReset();
  vi.stubGlobal("XMLHttpRequest", FakeRequest);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a JPEG the server refuses as not what its extension says", () => {
  it("is sent again as the picture alone, with the same options, and the client sees no refusal", async () => {
    browser.shrinkImage.mockResolvedValue(thePictureAlone());
    const { result, seen } = mount();
    const photo = aPhoto();

    act(() =>
      result.current.upload(photo, {
        practiceId: 7,
        documentType: "passport",
        purpose: " for the renewal ",
      }),
    );
    refuse(FakeRequest.all[0]);
    await waitFor(() => expect(FakeRequest.all).toHaveLength(2));

    expect(browser.shrinkImage).toHaveBeenCalledTimes(1);
    expect(browser.shrinkImage).toHaveBeenCalledWith(photo, MAX_SIZE_BYTES);
    const second = FakeRequest.all[1];
    expect(second.opened).toEqual({
      method: "POST",
      url: "/api/portal/documents/upload",
    });
    expect(second.withCredentials).toBe(true);
    expect(sentFile(second).name).toBe("IMG_0001.jpg");
    expect(sentFile(second).type).toBe("image/jpeg");
    expect(sentFile(second).size).toBe(512);
    expect(second.body!.get("practice_id")).toBe("7");
    expect(second.body!.get("document_type")).toBe("passport");
    expect(second.body!.get("document_purpose")).toBe("for the renewal");

    act(() => second.answer(200, ACCEPTED));
    await waitFor(() => expect(result.current.state.status).toBe("done"));
    expect(seen).not.toContain("error");
    expect(FakeRequest.all).toHaveLength(2);
  });

  it("shows the server's refusal when the picture alone is refused too, and stops there", async () => {
    browser.shrinkImage.mockResolvedValue(thePictureAlone());
    const { result } = mount();

    act(() => result.current.upload(aPhoto()));
    refuse(FakeRequest.all[0]);
    await waitFor(() => expect(FakeRequest.all).toHaveLength(2));
    refuse(FakeRequest.all[1]);

    await waitFor(() => expect(result.current.state.status).toBe("error"));
    expect(result.current.state).toEqual({
      status: "error",
      message: REFUSAL,
      httpStatus: 415,
    });
    expect(result.current.canRetry).toBe(false);
    expect(browser.shrinkImage).toHaveBeenCalledTimes(1);
    expect(FakeRequest.all).toHaveLength(2);
  });

  it.each([
    ["cannot read the picture", () => Promise.resolve(null)],
    ["fails while reading it", () => Promise.reject(new Error("no decode"))],
  ])(
    "shows the server's refusal when the browser %s",
    async (_label, outcome) => {
      browser.shrinkImage.mockImplementation(outcome);
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      refuse(FakeRequest.all[0]);

      await waitFor(() => expect(result.current.state.status).toBe("error"));
      expect(result.current.state).toEqual({
        status: "error",
        message: REFUSAL,
        httpStatus: 415,
      });
      expect(browser.shrinkImage).toHaveBeenCalledTimes(1);
      expect(FakeRequest.all).toHaveLength(1);
    },
  );

  it("keeps the retry of a transport failure on the picture alone, without reading the photo again", async () => {
    browser.shrinkImage.mockResolvedValue(thePictureAlone());
    const { result } = mount();

    act(() => result.current.upload(aPhoto(), { documentType: "passport" }));
    refuse(FakeRequest.all[0]);
    await waitFor(() => expect(FakeRequest.all).toHaveLength(2));
    act(() => FakeRequest.all[1].answer(503, ""));
    await waitFor(() => expect(result.current.state.status).toBe("error"));
    expect(result.current.canRetry).toBe(true);

    act(() => result.current.retry());

    expect(FakeRequest.all).toHaveLength(3);
    expect(sentFile(FakeRequest.all[2]).size).toBe(512);
    expect(FakeRequest.all[2].body!.get("document_type")).toBe("passport");
    expect(browser.shrinkImage).toHaveBeenCalledTimes(1);
  });
});

describe("what is not sent again", () => {
  it.each([
    ["a PDF", "scan.pdf", "application/pdf"],
    ["a PNG", "scan.png", "image/png"],
    ["a Word file", "letter.docx", WORD],
  ])("%s the server refuses with 415", async (_label, name, type) => {
    const { result } = mount();

    act(() => result.current.upload(fileOf(name, type)));
    refuse(FakeRequest.all[0]);

    await waitFor(() => expect(result.current.state.status).toBe("error"));
    expect(result.current.state).toMatchObject({
      message: REFUSAL,
      httpStatus: 415,
    });
    expect(browser.shrinkImage).not.toHaveBeenCalled();
    expect(FakeRequest.all).toHaveLength(1);
  });

  it.each([400, 401, 403, 409, 413, 422, 429, 500, 503])(
    "a JPEG the server answers with %i",
    async (status) => {
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      refuse(FakeRequest.all[0], status);

      await waitFor(() => expect(result.current.state.status).toBe("error"));
      expect(result.current.state).toMatchObject({ httpStatus: status });
      expect(browser.shrinkImage).not.toHaveBeenCalled();
      expect(FakeRequest.all).toHaveLength(1);
    },
  );
});

describe("a picture that is ready after the client has moved on", () => {
  function aPictureThatIsNotReadyYet() {
    let finish!: (picture: File | null) => void;
    browser.shrinkImage.mockReturnValue(
      new Promise<File | null>((resolve) => {
        finish = resolve;
      }),
    );
    return (picture: File | null) => act(async () => finish(picture));
  }

  it("is not sent after a reset", async () => {
    const finish = aPictureThatIsNotReadyYet();
    const { result } = mount();

    act(() => result.current.upload(aPhoto()));
    refuse(FakeRequest.all[0]);
    expect(browser.shrinkImage).toHaveBeenCalledTimes(1);
    act(() => result.current.reset());
    await finish(thePictureAlone());

    expect(FakeRequest.all).toHaveLength(1);
    expect(result.current.state.status).toBe("idle");
  });

  it("is not sent after another upload has started, and does not touch that upload", async () => {
    const finish = aPictureThatIsNotReadyYet();
    const { result } = mount();

    act(() => result.current.upload(aPhoto()));
    refuse(FakeRequest.all[0]);
    act(() => result.current.upload(fileOf("scan.pdf", "application/pdf")));
    expect(FakeRequest.all).toHaveLength(2);
    await finish(null);
    await finish(thePictureAlone());

    expect(FakeRequest.all).toHaveLength(2);
    expect(result.current.state.status).toBe("validating");
    expect(sentFile(FakeRequest.all[1]).name).toBe("scan.pdf");
  });

  it("is not even asked for when the refusal arrives after a reset", () => {
    const { result } = mount();

    act(() => result.current.upload(aPhoto()));
    act(() => result.current.reset());
    refuse(FakeRequest.all[0]);

    expect(browser.shrinkImage).not.toHaveBeenCalled();
    expect(FakeRequest.all).toHaveLength(1);
  });
});

describe("what the usage events say of a photo sent twice", () => {
  const gtag = vi.fn();
  beforeEach(() => {
    gtag.mockReset();
    Object.assign(window, { gtag });
    window.history.replaceState({}, "", "/portal/vault");
  });
  afterEach(() => {
    delete (window as Window & { gtag?: unknown }).gtag;
  });

  it("one attempt that failed at the request and one that completed, with no file name", async () => {
    browser.shrinkImage.mockResolvedValue(thePictureAlone());
    const { result } = mount();

    act(() => result.current.upload(aPhoto()));
    refuse(FakeRequest.all[0]);
    await waitFor(() => expect(FakeRequest.all).toHaveLength(2));
    act(() => FakeRequest.all[1].answer(200, ACCEPTED));
    await waitFor(() => expect(result.current.state.status).toBe("done"));

    expect(gtag.mock.calls.map((call) => call[1])).toEqual([
      "portal_action_started",
      "portal_action_failed",
      "portal_action_started",
      "portal_action_completed",
    ]);
    expect(gtag.mock.calls[1][2].failure_class).toBe("request");
    expect(JSON.stringify(gtag.mock.calls)).not.toContain("IMG_0001");
  });
});

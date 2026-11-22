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
  fail() {
    this.onerror?.();
  }
  progress(loaded: number, total: number) {
    this.upload.onprogress?.({
      lengthComputable: true,
      loaded,
      total,
    } as unknown as ProgressEvent);
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
  return { result: view.result, unmount: view.unmount, seen };
}

/** The answers a request can still get after the client has moved on. */
const LATE_ANSWERS: [string, (request: FakeRequest) => void][] = [
  ["a refusal", (r) => r.answer(415, JSON.stringify({ detail: REFUSAL }))],
  ["a conflict", (r) => r.answer(409, JSON.stringify({ detail: "Taken" }))],
  ["a success", (r) => r.answer(200, ACCEPTED)],
  ["an answer that is not the agreed one", (r) => r.answer(200, "{}")],
  ["a server error", (r) => r.answer(500, "")],
  ["a network error", (r) => r.fail()],
  ["a progress event", (r) => r.progress(1, 2)],
];

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

  async function thePictureAloneMetATransportFailure() {
    browser.shrinkImage.mockResolvedValue(thePictureAlone());
    const mounted = mount();
    act(() =>
      mounted.result.current.upload(aPhoto(), { documentType: "passport" }),
    );
    refuse(FakeRequest.all[0]);
    await waitFor(() => expect(FakeRequest.all).toHaveLength(2));
    act(() => FakeRequest.all[1].answer(503, ""));
    await waitFor(() =>
      expect(mounted.result.current.state.status).toBe("error"),
    );
    expect(mounted.result.current.canRetry).toBe(true);
    return mounted;
  }

  it("a retry after a transport failure sends the picture alone again, and a refusal of it is final", async () => {
    const { result } = await thePictureAloneMetATransportFailure();

    act(() => result.current.retry());
    expect(FakeRequest.all).toHaveLength(3);
    expect(sentFile(FakeRequest.all[2]).size).toBe(512);
    expect(FakeRequest.all[2].body!.get("document_type")).toBe("passport");
    refuse(FakeRequest.all[2]);

    await waitFor(() => expect(result.current.state.status).toBe("error"));
    expect(result.current.state).toEqual({
      status: "error",
      message: REFUSAL,
      httpStatus: 415,
    });
    expect(result.current.canRetry).toBe(false);
    expect(browser.shrinkImage).toHaveBeenCalledTimes(1);
    expect(FakeRequest.all).toHaveLength(3);
  });

  it("stays the picture alone through a second transport failure and its retry", async () => {
    const { result } = await thePictureAloneMetATransportFailure();

    act(() => result.current.retry());
    act(() => FakeRequest.all[2].answer(503, ""));
    await waitFor(() => expect(result.current.canRetry).toBe(true));
    act(() => result.current.retry());
    expect(sentFile(FakeRequest.all[3]).size).toBe(512);
    refuse(FakeRequest.all[3]);

    await waitFor(() => expect(result.current.state.status).toBe("error"));
    expect(result.current.state).toMatchObject({ httpStatus: 415 });
    expect(browser.shrinkImage).toHaveBeenCalledTimes(1);
    expect(FakeRequest.all).toHaveLength(4);
  });

  it("a photo that first met a transport failure is still re-encoded once when its retry is refused", async () => {
    browser.shrinkImage.mockResolvedValue(thePictureAlone());
    const { result } = mount();

    act(() => result.current.upload(aPhoto()));
    act(() => FakeRequest.all[0].answer(503, ""));
    await waitFor(() => expect(result.current.canRetry).toBe(true));
    act(() => result.current.retry());
    expect(sentFile(FakeRequest.all[1]).size).toBe(2048);
    refuse(FakeRequest.all[1]);
    await waitFor(() => expect(FakeRequest.all).toHaveLength(3));

    expect(sentFile(FakeRequest.all[2]).size).toBe(512);
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

  it("is not sent after the page has gone", async () => {
    const finish = aPictureThatIsNotReadyYet();
    const { result, unmount } = mount();

    act(() => result.current.upload(aPhoto()));
    refuse(FakeRequest.all[0]);
    unmount();
    await finish(thePictureAlone());

    expect(FakeRequest.all).toHaveLength(1);
  });
});

describe("an answer that arrives after the client has moved on", () => {
  it.each(LATE_ANSWERS)(
    "%s after a reset leaves the upload idle and asks for no picture",
    (_label, late) => {
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      act(() => result.current.reset());
      act(() => late(FakeRequest.all[0]));

      expect(result.current.state).toEqual({ status: "idle" });
      expect(result.current.canRetry).toBe(false);
      expect(browser.shrinkImage).not.toHaveBeenCalled();
      expect(FakeRequest.all).toHaveLength(1);
    },
  );

  it.each(LATE_ANSWERS)(
    "%s for the picture alone after a reset leaves the upload idle",
    async (_label, late) => {
      browser.shrinkImage.mockResolvedValue(thePictureAlone());
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      refuse(FakeRequest.all[0]);
      await waitFor(() => expect(FakeRequest.all).toHaveLength(2));
      act(() => result.current.reset());
      act(() => late(FakeRequest.all[1]));

      expect(result.current.state).toEqual({ status: "idle" });
      expect(result.current.canRetry).toBe(false);
      expect(FakeRequest.all).toHaveLength(2);
    },
  );

  it.each(LATE_ANSWERS)(
    "%s for an earlier upload leaves a newer one in progress as it is",
    (_label, late) => {
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      act(() => result.current.upload(fileOf("scan.pdf", "application/pdf")));
      act(() => FakeRequest.all[1].progress(1, 4));
      act(() => late(FakeRequest.all[0]));

      expect(result.current.state).toEqual({
        status: "uploading",
        progress: 25,
      });
      expect(browser.shrinkImage).not.toHaveBeenCalled();
      expect(FakeRequest.all).toHaveLength(2);
    },
  );

  it.each(LATE_ANSWERS)(
    "%s for an earlier upload does not take the retry of a newer one that failed",
    (_label, late) => {
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      act(() =>
        result.current.upload(fileOf("scan.pdf", "application/pdf"), {
          documentType: "passport",
        }),
      );
      act(() => FakeRequest.all[1].fail());
      act(() => late(FakeRequest.all[0]));

      expect(result.current.state).toEqual({
        status: "error",
        message: "Network error",
      });
      expect(result.current.canRetry).toBe(true);
      act(() => result.current.retry());
      expect(FakeRequest.all).toHaveLength(3);
      expect(sentFile(FakeRequest.all[2]).name).toBe("scan.pdf");
      expect(FakeRequest.all[2].body!.get("document_type")).toBe("passport");
      expect(browser.shrinkImage).not.toHaveBeenCalled();
    },
  );

  it.each(LATE_ANSWERS)(
    "%s for the request a retry replaced leaves the retry as it is",
    (_label, late) => {
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      act(() => FakeRequest.all[0].fail());
      act(() => result.current.retry());
      act(() => FakeRequest.all[1].progress(3, 4));
      act(() => late(FakeRequest.all[0]));

      expect(result.current.state).toEqual({
        status: "uploading",
        progress: 75,
      });
      act(() => FakeRequest.all[1].fail());
      expect(result.current.canRetry).toBe(true);
      expect(browser.shrinkImage).not.toHaveBeenCalled();
    },
  );

  it("nothing is shown for an answer that arrives after the page has gone", () => {
    const { result, unmount, seen } = mount();

    act(() => result.current.upload(aPhoto()));
    const before = seen.length;
    unmount();
    refuse(FakeRequest.all[0]);

    expect(seen).toHaveLength(before);
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

  it.each([
    ["a server error", 500, "", "portal_action_failed"],
    ["a success", 200, ACCEPTED, "portal_action_completed"],
  ])(
    "a late answer still closes its attempt: %s after a reset",
    (_label, status, body, event) => {
      const { result } = mount();

      act(() => result.current.upload(aPhoto()));
      act(() => result.current.reset());
      act(() => FakeRequest.all[0].answer(status, body));

      expect(gtag.mock.calls.map((call) => call[1])).toEqual([
        "portal_action_started",
        event,
      ]);
      expect(result.current.state).toEqual({ status: "idle" });
    },
  );
});

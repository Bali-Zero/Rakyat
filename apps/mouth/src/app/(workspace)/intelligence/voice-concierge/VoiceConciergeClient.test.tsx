import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VOICE_CONCIERGE_MAX_MESSAGE_LENGTH } from "@/lib/voice-concierge-limits";
import { VoiceConciergeClient } from "./VoiceConciergeClient";

const originalMediaRecorder = globalThis.MediaRecorder;
const originalMediaDevices = navigator.mediaDevices;
const originalAudio = globalThis.Audio;
const originalSpeechSynthesis = window.speechSynthesis;
const originalSpeechSynthesisUtterance = window.SpeechSynthesisUtterance;
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

function ttsProfileStatus(
  overrides: Partial<{
    active_profile: "high_quality_offline" | "browser_realtime";
    active_provider: string;
    quality: "high_quality" | "realtime";
    latency_class: "offline" | "interactive";
  }> = {},
): object {
  const activeProfile = overrides.active_profile ?? "high_quality_offline";
  const activeProvider =
    overrides.active_provider ??
    (activeProfile === "browser_realtime"
      ? "browser-web-speech-local"
      : "chatterbox-v3");
  const quality =
    overrides.quality ??
    (activeProfile === "browser_realtime" ? "realtime" : "high_quality");
  const latencyClass =
    overrides.latency_class ??
    (activeProfile === "browser_realtime" ? "interactive" : "offline");
  const policy = {
    requires_network: false,
    allows_cloud_fallback: false,
    pii_boundary: "local_only",
  };

  return {
    active_profile: activeProfile,
    active_provider: activeProvider,
    quality,
    latency_class: latencyClass,
    fallback_policy: "fail_closed",
    profiles: {
      high_quality_offline: {
        profile: "high_quality_offline",
        provider: "chatterbox-v3",
        quality: "high_quality",
        latency_class: "offline",
        available: true,
        detail: "ready",
        policy,
      },
      browser_realtime: {
        profile: "browser_realtime",
        provider: "browser-web-speech-local",
        quality: "realtime",
        latency_class: "interactive",
        available: false,
        detail: "client must confirm a browser localService voice",
        policy,
      },
    },
  };
}

function readyLocalAudioStatus(): object {
  return {
    browser_speech_provider: "disabled",
    text_concierge_provider: "local-demo",
    tts_profile: ttsProfileStatus(),
    local_audio: {
      enabled: true,
      ready: true,
      roundtrip_ready: true,
      turn_detection_ready: true,
      source: "backend",
      providers: {
        stt: {
          name: "whisper.cpp",
          available: true,
          detail: "ready",
          policy: {
            requires_network: false,
            allows_cloud_fallback: false,
            pii_boundary: "local_only",
          },
        },
        vad: {
          name: "silero-vad",
          available: true,
          detail: "ready",
          policy: {
            requires_network: false,
            allows_cloud_fallback: false,
            pii_boundary: "local_only",
          },
        },
        tts: {
          name: "chatterbox-v3",
          available: true,
          detail: "ready",
          policy: {
            requires_network: false,
            allows_cloud_fallback: false,
            pii_boundary: "local_only",
          },
        },
      },
      constraints: ["local_only"],
    },
  };
}

function roundtripReadyLocalAudioStatus(): object {
  const status = readyLocalAudioStatus() as {
    local_audio: {
      ready: boolean;
      roundtrip_ready: boolean;
      turn_detection_ready: boolean;
      providers: { vad: { available: boolean; detail: string } };
    };
  };

  status.local_audio.ready = false;
  status.local_audio.roundtrip_ready = true;
  status.local_audio.turn_detection_ready = false;
  status.local_audio.providers.vad.available = false;
  status.local_audio.providers.vad.detail = "runtime installed, not wired";
  return status;
}

function browserRealtimeStatus(): object {
  const status = roundtripReadyLocalAudioStatus() as {
    browser_speech_provider: "disabled" | "web-speech-local";
    tts_profile: object;
  };
  status.browser_speech_provider = "web-speech-local";
  status.tts_profile = ttsProfileStatus({
    active_profile: "browser_realtime",
  });
  return status;
}

function mockReadyStatusFetch(): void {
  vi.mocked(global.fetch).mockImplementation(async (input) => {
    if (String(input).endsWith("/status")) {
      return new Response(JSON.stringify(readyLocalAudioStatus()), {
        status: 200,
      });
    }

    return new Response(JSON.stringify({ error: "unexpected request" }), {
      status: 500,
    });
  });
}

describe("VoiceConciergeClient", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(global.fetch).mockImplementation(async (input) => {
      if (String(input).endsWith("/status")) {
        return new Response(
          JSON.stringify({
            browser_speech_provider: "disabled",
            text_concierge_provider: "local-demo",
            local_audio: {
              enabled: false,
              ready: false,
              roundtrip_ready: false,
              turn_detection_ready: false,
              source: "disabled",
              providers: {
                stt: {
                  name: "whisper.cpp",
                  available: false,
                  detail: "local audio disabled",
                  policy: {
                    requires_network: false,
                    allows_cloud_fallback: false,
                    pii_boundary: "local_only",
                  },
                },
                vad: {
                  name: "silero-vad",
                  available: false,
                  detail: "local audio disabled",
                  policy: {
                    requires_network: false,
                    allows_cloud_fallback: false,
                    pii_boundary: "local_only",
                  },
                },
                tts: {
                  name: "chatterbox-v3",
                  available: false,
                  detail: "local audio disabled",
                  policy: {
                    requires_network: false,
                    allows_cloud_fallback: false,
                    pii_boundary: "local_only",
                  },
                },
              },
              constraints: ["local_only"],
            },
            tts_profile: ttsProfileStatus(),
          }),
          { status: 200 },
        );
      }

      return new Response(
        JSON.stringify({
          answer: "Start with the KBLI and zoning check.",
          intent: "company",
          risk_level: "medium",
          next_action: "collect_non_pii_context",
          quick_replies: ["Ask about KBLI", "Check zoning"],
          safety_note: "No PII.",
          mode: "demo",
          provider: "local-demo",
        }),
        { status: 200 },
      );
    });
  });

  afterEach(() => {
    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: originalMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: originalMediaDevices,
    });
    Object.defineProperty(globalThis, "Audio", {
      configurable: true,
      value: originalAudio,
    });
    Object.defineProperty(window, "speechSynthesis", {
      configurable: true,
      value: originalSpeechSynthesis,
    });
    Object.defineProperty(window, "SpeechSynthesisUtterance", {
      configurable: true,
      value: originalSpeechSynthesisUtterance,
    });
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: originalCreateObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: originalRevokeObjectURL,
    });
  });

  it("renders the voice concierge workbench", async () => {
    render(<VoiceConciergeClient />);

    expect(
      screen.getByRole("heading", { name: "Voice Concierge" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Audio stack")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("Audio gated").length).toBeGreaterThan(0);
    });
    expect(
      screen.getByRole("button", {
        name: "Voice input gated until local audio roundtrip is ready",
      }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("sends a typed turn and displays the structured response", async () => {
    const user = userEvent.setup();
    render(<VoiceConciergeClient />);

    await user.type(
      screen.getByLabelText("Concierge prompt"),
      "Can I open a cafe in Bali?",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(
        screen.getByText("Start with the KBLI and zoning check."),
      ).toBeInTheDocument();
    });
    expect(screen.getAllByText("Audio gated").length).toBeGreaterThan(0);
    expect(screen.getByText("company")).toBeInTheDocument();
    expect(screen.getByText("collect_non_pii_context")).toBeInTheDocument();
    expect(screen.getByText("Ask about KBLI")).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/lab/voice-concierge",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("plays assistant responses through the local synthesize bridge when roundtrip is ready", async () => {
    const user = userEvent.setup();
    const playAudio = vi.fn().mockResolvedValue(undefined);
    const pauseAudio = vi.fn();
    const createdAudio: Array<{
      src: string;
      onended: (() => void) | null;
      onerror: (() => void) | null;
      play: () => Promise<void>;
      pause: () => void;
    }> = [];

    class MockAudio {
      src: string;
      onended: (() => void) | null = null;
      onerror: (() => void) | null = null;
      play = playAudio;
      pause = pauseAudio;

      constructor(src: string) {
        this.src = src;
        createdAudio.push(this);
      }
    }

    Object.defineProperty(globalThis, "Audio", {
      configurable: true,
      value: MockAudio,
    });
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:voice-concierge-tts"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });

    vi.mocked(global.fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return new Response(JSON.stringify(roundtripReadyLocalAudioStatus()), {
          status: 200,
        });
      }

      if (url.endsWith("/synthesize")) {
        expect(init?.method).toBe("POST");
        expect(init?.headers).toEqual({ "Content-Type": "application/json" });
        expect(JSON.parse(String(init?.body))).toEqual({
          text: "Triage PMA pronto. Parti da KBLI e zoning.",
          language: "it",
        });
        return new Response("RIFF", {
          status: 200,
          headers: { "Content-Type": "audio/wav" },
        });
      }

      return new Response(
        JSON.stringify({
          answer:
            "Per una PT PMA, i primi controlli utili sono attivita, KBLI, limiti di proprieta straniera, indirizzo/zoning e permessi extra.",
          spoken_answer: "Triage PMA pronto. Parti da KBLI e zoning.",
          intent: "company",
          risk_level: "medium",
          next_action: "collect_non_pii_context",
          quick_replies: ["Ask about shareholders"],
          safety_note: "No PII.",
          mode: "demo",
          provider: "local-demo",
        }),
        { status: 200 },
      );
    });

    render(<VoiceConciergeClient />);

    await user.type(
      screen.getByLabelText("Concierge prompt"),
      "Can I open a PMA?",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Per una PT PMA, i primi controlli utili sono attivita, KBLI, limiti di proprieta straniera, indirizzo/zoning e permessi extra.",
        ),
      ).toBeInTheDocument();
      expect(playAudio).toHaveBeenCalledTimes(1);
    });
    const synthesizedAudio = vi.mocked(URL.createObjectURL).mock.calls[0]?.[0];
    expect(synthesizedAudio).toMatchObject({
      size: 4,
      type: "audio/wav",
    });
    expect(createdAudio[0]?.src).toBe("blob:voice-concierge-tts");
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/lab/voice-concierge/synthesize",
      expect.objectContaining({ method: "POST" }),
    );

    await act(async () => {
      createdAudio[0]?.onended?.();
    });
    await waitFor(() => {
      expect(URL.revokeObjectURL).toHaveBeenCalledWith(
        "blob:voice-concierge-tts",
      );
    });
  });

  it("uses spoken_answer through browser realtime TTS without backend synthesis", async () => {
    const user = userEvent.setup();
    const speak = vi.fn((utterance: SpeechSynthesisUtterance) => {
      utterance.onend?.({} as SpeechSynthesisEvent);
    });
    const cancel = vi.fn();

    class MockUtterance {
      text: string;
      voice: SpeechSynthesisVoice | null = null;
      lang = "";
      onend: ((event: SpeechSynthesisEvent) => void) | null = null;
      onerror: ((event: SpeechSynthesisErrorEvent) => void) | null = null;

      constructor(text: string) {
        this.text = text;
      }
    }

    Object.defineProperty(window, "SpeechSynthesisUtterance", {
      configurable: true,
      value: MockUtterance,
    });
    Object.defineProperty(window, "speechSynthesis", {
      configurable: true,
      value: {
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        cancel,
        getVoices: vi.fn(() => [
          {
            name: "Local Alice",
            lang: "it-IT",
            localService: true,
          } as SpeechSynthesisVoice,
        ]),
        speak,
      },
    });

    vi.mocked(global.fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return new Response(JSON.stringify(browserRealtimeStatus()), {
          status: 200,
        });
      }
      if (url.endsWith("/synthesize")) {
        return new Response(
          JSON.stringify({ error: "unexpected synthesize" }),
          {
            status: 500,
          },
        );
      }
      return new Response(
        JSON.stringify({
          answer:
            "Per una PT PMA, verifica attivita, KBLI, limiti stranieri, zoning e permessi.",
          spoken_answer: "Triage PMA pronto. Parti da KBLI e zoning.",
          intent: "company",
          risk_level: "medium",
          next_action: "collect_non_pii_context",
          quick_replies: ["Ask about shareholders"],
          safety_note: "No PII.",
          mode: "demo",
          provider: "local-demo",
        }),
        { status: 200 },
      );
    });

    render(<VoiceConciergeClient />);

    await user.type(
      screen.getByLabelText("Concierge prompt"),
      "Can I open a PMA?",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(speak).toHaveBeenCalledTimes(1);
    });
    expect(speak.mock.calls[0]?.[0]).toMatchObject({
      text: "Triage PMA pronto. Parti da KBLI e zoning.",
      lang: "it-IT",
    });
    expect(global.fetch).not.toHaveBeenCalledWith(
      "/api/lab/voice-concierge/synthesize",
      expect.anything(),
    );
  });

  it("fails closed when realtime TTS has no local browser voice", async () => {
    const user = userEvent.setup();
    const speak = vi.fn();

    Object.defineProperty(window, "SpeechSynthesisUtterance", {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(window, "speechSynthesis", {
      configurable: true,
      value: {
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        cancel: vi.fn(),
        getVoices: vi.fn(() => [
          {
            name: "Remote Voice",
            lang: "en-US",
            localService: false,
          } as SpeechSynthesisVoice,
        ]),
        speak,
      },
    });

    vi.mocked(global.fetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return new Response(JSON.stringify(browserRealtimeStatus()), {
          status: 200,
        });
      }
      if (url.endsWith("/synthesize")) {
        return new Response(
          JSON.stringify({ error: "unexpected synthesize" }),
          {
            status: 500,
          },
        );
      }
      return new Response(
        JSON.stringify({
          answer:
            "Keep the details non-PII and start with the service category.",
          spoken_answer: "Client Maria Rossi needs a PT PMA.",
          intent: "company",
          risk_level: "medium",
          next_action: "collect_non_pii_context",
          quick_replies: ["Ask about shareholders"],
          safety_note: "No PII.",
          mode: "demo",
          provider: "local-demo",
        }),
        { status: 200 },
      );
    });

    render(<VoiceConciergeClient />);

    await user.type(
      screen.getByLabelText("Concierge prompt"),
      "Can I open a PMA?",
    );
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Keep the details non-PII and start with the service category.",
        ),
      ).toBeInTheDocument();
    });
    expect(speak).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalledWith(
      "/api/lab/voice-concierge/synthesize",
      expect.anything(),
    );
    expect(
      screen.getByRole("button", { name: "Read latest answer" }),
    ).toBeDisabled();
  });

  it("does not play stale synthesized audio after a newer answer wins the race", async () => {
    const user = userEvent.setup();
    const playAudio = vi.fn().mockResolvedValue(undefined);
    const pauseAudio = vi.fn();
    const createdAudio: Array<{
      src: string;
      play: () => Promise<void>;
      pause: () => void;
    }> = [];
    let conciergeCalls = 0;
    let resolveFirstSynthesis: ((response: Response) => void) | undefined;

    class MockAudio {
      src: string;
      onended: (() => void) | null = null;
      onerror: (() => void) | null = null;
      play = playAudio;
      pause = pauseAudio;

      constructor(src: string) {
        this.src = src;
        createdAudio.push(this);
      }
    }

    Object.defineProperty(globalThis, "Audio", {
      configurable: true,
      value: MockAudio,
    });
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:latest-voice"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });

    vi.mocked(global.fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return new Response(JSON.stringify(readyLocalAudioStatus()), {
          status: 200,
        });
      }

      if (url.endsWith("/synthesize")) {
        const body = JSON.parse(String(init?.body)) as { text: string };
        if (body.text === "First answer") {
          return new Promise<Response>((resolve) => {
            resolveFirstSynthesis = resolve;
          });
        }

        return new Response("RIFF-latest", {
          status: 200,
          headers: { "Content-Type": "audio/wav" },
        });
      }

      conciergeCalls += 1;
      const answer = conciergeCalls === 1 ? "First answer" : "Second answer";
      return new Response(
        JSON.stringify({
          answer,
          intent: "company",
          risk_level: "medium",
          next_action: "collect_non_pii_context",
          quick_replies: ["Ask about shareholders"],
          safety_note: "No PII.",
          mode: "demo",
          provider: "local-demo",
        }),
        { status: 200 },
      );
    });

    render(<VoiceConciergeClient />);

    await user.type(screen.getByLabelText("Concierge prompt"), "First");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(screen.getByText("First answer")).toBeInTheDocument();
      expect(resolveFirstSynthesis).toBeTypeOf("function");
    });

    await user.type(screen.getByLabelText("Concierge prompt"), "Second");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(screen.getByText("Second answer")).toBeInTheDocument();
      expect(playAudio).toHaveBeenCalledTimes(1);
    });

    await act(async () => {
      resolveFirstSynthesis?.(
        new Response("RIFF-stale", {
          status: 200,
          headers: { "Content-Type": "audio/wav" },
        }),
      );
    });

    await waitFor(() => {
      expect(playAudio).toHaveBeenCalledTimes(1);
      expect(createdAudio).toHaveLength(1);
      expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    });
  });

  it("records browser audio, transcribes locally, and submits the transcript", async () => {
    const user = userEvent.setup();
    const stopTrack = vi.fn();

    class MockMediaRecorder {
      static isTypeSupported(type: string): boolean {
        return type === "audio/webm;codecs=opus";
      }

      mimeType: string;
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      state: RecordingState = "inactive";

      constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
        this.mimeType = options?.mimeType ?? "audio/webm";
      }

      start(): void {
        this.state = "recording";
        this.ondataavailable?.({
          data: new Blob(["voice-audio"], { type: this.mimeType }),
        } as BlobEvent);
      }

      stop(): void {
        this.state = "inactive";
        this.onstop?.();
      }
    }

    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: MockMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: stopTrack }],
        }),
      },
    });

    vi.mocked(global.fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return new Response(
          JSON.stringify({
            browser_speech_provider: "disabled",
            text_concierge_provider: "local-demo",
            local_audio: {
              enabled: true,
              ready: false,
              roundtrip_ready: true,
              turn_detection_ready: false,
              source: "backend",
              providers: {
                stt: {
                  name: "whisper.cpp",
                  available: true,
                  detail: "ready",
                  policy: {
                    requires_network: false,
                    allows_cloud_fallback: false,
                    pii_boundary: "local_only",
                  },
                },
                vad: {
                  name: "silero-vad",
                  available: false,
                  detail: "runtime installed, not wired",
                  policy: {
                    requires_network: false,
                    allows_cloud_fallback: false,
                    pii_boundary: "local_only",
                  },
                },
                tts: {
                  name: "chatterbox-v3",
                  available: true,
                  detail: "ready",
                  policy: {
                    requires_network: false,
                    allows_cloud_fallback: false,
                    pii_boundary: "local_only",
                  },
                },
              },
              constraints: ["local_only"],
            },
          }),
          { status: 200 },
        );
      }

      if (url.endsWith("/transcribe")) {
        expect(init?.method).toBe("POST");
        expect(init?.body).toBeInstanceOf(FormData);
        expect((init?.body as FormData).get("file")).toBeTruthy();
        expect((init?.body as FormData).get("language")).toBeNull();
        return new Response(
          JSON.stringify({
            text: "I need help with a PMA company",
            language: "en",
            duration_seconds: 1.2,
            provider: "whisper.cpp",
            constraints: ["local_only"],
          }),
          { status: 200 },
        );
      }

      const body = JSON.parse(String(init?.body));
      expect(body.message).toBe("I need help with a PMA company");
      return new Response(
        JSON.stringify({
          answer: "Use a PMA setup flow.",
          intent: "company",
          risk_level: "medium",
          next_action: "collect_non_pii_context",
          quick_replies: ["Ask about shareholders"],
          safety_note: "No PII.",
          mode: "demo",
          provider: "local-demo",
        }),
        { status: 200 },
      );
    });

    render(<VoiceConciergeClient />);

    const voiceButton = await screen.findByRole("button", {
      name: "Start voice input",
    });
    await waitFor(() => expect(voiceButton).toBeEnabled());
    await user.click(voiceButton);
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({
      audio: true,
    });

    await user.click(screen.getByRole("button", { name: "Stop voice input" }));

    await waitFor(() => {
      expect(
        screen.getByText("I need help with a PMA company"),
      ).toBeInTheDocument();
      expect(screen.getByText("Use a PMA setup flow.")).toBeInTheDocument();
    });
    expect(stopTrack).toHaveBeenCalled();
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/lab/voice-concierge/transcribe",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("serializes recorder startup while microphone permission is pending", async () => {
    const user = userEvent.setup();
    const stopTrack = vi.fn();
    const startRecorder = vi.fn();
    let resolveStream: ((stream: MediaStream) => void) | undefined;

    class PendingMediaRecorder {
      static isTypeSupported(): boolean {
        return true;
      }

      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      state: RecordingState = "inactive";

      start(): void {
        this.state = "recording";
        startRecorder();
      }

      stop(): void {
        this.state = "inactive";
        this.onstop?.();
      }
    }

    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: PendingMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn(
          () =>
            new Promise<MediaStream>((resolve) => {
              resolveStream = resolve;
            }),
        ),
      },
    });
    mockReadyStatusFetch();

    render(<VoiceConciergeClient />);

    const voiceButton = await screen.findByRole("button", {
      name: "Start voice input",
    });
    await user.click(voiceButton);
    await user.click(voiceButton);

    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);

    resolveStream?.({
      getTracks: () => [{ stop: stopTrack }],
    } as unknown as MediaStream);

    await waitFor(() => {
      expect(startRecorder).toHaveBeenCalledTimes(1);
      expect(
        screen.getByRole("button", { name: "Stop voice input" }),
      ).toBeEnabled();
    });
  });

  it("stops a late microphone stream when unmounted during permission", async () => {
    const user = userEvent.setup();
    const stopTrack = vi.fn();
    let resolveStream: ((stream: MediaStream) => void) | undefined;

    class PendingMediaRecorder {
      static isTypeSupported(): boolean {
        return true;
      }

      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      state: RecordingState = "inactive";
      start(): void {
        this.state = "recording";
      }
      stop(): void {
        this.state = "inactive";
      }
    }

    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: PendingMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn(
          () =>
            new Promise<MediaStream>((resolve) => {
              resolveStream = resolve;
            }),
        ),
      },
    });
    mockReadyStatusFetch();

    const view = render(<VoiceConciergeClient />);

    await user.click(
      await screen.findByRole("button", { name: "Start voice input" }),
    );
    view.unmount();

    resolveStream?.({
      getTracks: () => [{ stop: stopTrack }],
    } as unknown as MediaStream);

    await waitFor(() => {
      expect(stopTrack).toHaveBeenCalledTimes(1);
    });
    expect(global.fetch).not.toHaveBeenCalledWith(
      "/api/lab/voice-concierge/transcribe",
      expect.anything(),
    );
  });

  it("ignores duplicate stop while the recorder is already stopping", async () => {
    const user = userEvent.setup();
    const stopTrack = vi.fn();
    const stopRecorder = vi.fn();

    class SlowStopMediaRecorder {
      static isTypeSupported(): boolean {
        return true;
      }

      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      state: RecordingState = "inactive";

      start(): void {
        this.state = "recording";
      }

      stop(): void {
        if (this.state === "inactive") {
          throw new DOMException("inactive", "InvalidStateError");
        }
        this.state = "inactive";
        stopRecorder();
      }
    }

    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: SlowStopMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: stopTrack }],
        }),
      },
    });
    mockReadyStatusFetch();

    render(<VoiceConciergeClient />);

    await user.click(
      await screen.findByRole("button", { name: "Start voice input" }),
    );
    const stopButton = await screen.findByRole("button", {
      name: "Stop voice input",
    });
    await user.click(stopButton);
    await user.click(stopButton);

    expect(stopRecorder).toHaveBeenCalledTimes(1);
  });

  it("disables typed submit while a recording is active", async () => {
    const user = userEvent.setup();
    const stopTrack = vi.fn();

    class ActiveMediaRecorder {
      static isTypeSupported(): boolean {
        return true;
      }

      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      state: RecordingState = "inactive";

      start(): void {
        this.state = "recording";
      }

      stop(): void {
        this.state = "inactive";
        this.onstop?.();
      }
    }

    Object.defineProperty(globalThis, "MediaRecorder", {
      configurable: true,
      value: ActiveMediaRecorder,
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: stopTrack }],
        }),
      },
    });
    mockReadyStatusFetch();

    render(<VoiceConciergeClient />);

    await user.type(screen.getByLabelText("Concierge prompt"), "Typed turn");
    await user.click(
      await screen.findByRole("button", { name: "Start voice input" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    });
    expect(global.fetch).not.toHaveBeenCalledWith(
      "/api/lab/voice-concierge",
      expect.objectContaining({ method: "POST" }),
    );
  });

  describe("a turn that fails never poisons the conversation", () => {
    type Outcome = () => Response | Promise<Response>;

    const CONTROL_QUESTION = "Can I open a cafe in Bali?";
    const CONTROL_ANSWER = "Start with the KBLI and zoning check.";
    const FAILED_QUESTION = "What about the second location?";
    const FALLBACK_ERROR = "The concierge could not answer. Try again.";

    function wellFormed(answer: string): Response {
      return new Response(
        JSON.stringify({
          answer,
          intent: "company",
          risk_level: "medium",
          next_action: "collect_non_pii_context",
          quick_replies: ["Ask about KBLI"],
          safety_note: "No PII.",
          mode: "demo",
          provider: "local-demo",
        }),
        { status: 200 },
      );
    }

    function jsonResponse(body: unknown, status: number): Response {
      return new Response(JSON.stringify(body), { status });
    }

    // Answers the concierge route from a queue and records every request body;
    // everything else (the status probe) keeps the default mock.
    function mockConcierge(queue: Outcome[]): {
      bodies: Array<{
        message: string;
        history: Array<{ role: string; content: string }>;
      }>;
    } {
      const base = vi.mocked(global.fetch).getMockImplementation();
      const bodies: Array<{
        message: string;
        history: Array<{ role: string; content: string }>;
      }> = [];
      vi.mocked(global.fetch).mockImplementation(async (input, init) => {
        if (String(input) !== "/api/lab/voice-concierge") {
          return base ? base(input, init) : new Response("{}", { status: 500 });
        }
        bodies.push(JSON.parse(String(init?.body)));
        const next = queue.shift();
        if (!next) throw new Error("unexpected extra concierge request");
        return next();
      });
      return { bodies };
    }

    function promptBox(): HTMLTextAreaElement {
      return screen.getByLabelText("Concierge prompt") as HTMLTextAreaElement;
    }

    function listHas(text: string): boolean {
      return screen.queryByText(text, { selector: "div" }) !== null;
    }

    async function send(text: string): Promise<void> {
      fireEvent.change(promptBox(), { target: { value: text } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    }

    async function sendControlTurn(): Promise<void> {
      await send(CONTROL_QUESTION);
      await waitFor(() => expect(listHas(CONTROL_ANSWER)).toBe(true));
    }

    const failures: Array<[string, Outcome, string]> = [
      [
        "a 400 message too long",
        () => jsonResponse({ error: "message too long" }, 400),
        "message too long",
      ],
      [
        "a 400 pii_not_allowed with a safety note",
        () =>
          jsonResponse(
            { error: "pii_not_allowed", safety_note: "Remove identifiers." },
            400,
          ),
        "Remove identifiers.",
      ],
      [
        "a body that cannot be parsed",
        () =>
          ({
            ok: true,
            status: 200,
            json: () => Promise.reject(new SyntaxError("Unexpected token")),
          }) as unknown as Response,
        FALLBACK_ERROR,
      ],
      [
        "a truncated JSON body",
        () => new Response('{"answer": "Start with', { status: 200 }),
        FALLBACK_ERROR,
      ],
      [
        "an abort",
        () => {
          throw new DOMException("The operation was aborted.", "AbortError");
        },
        FALLBACK_ERROR,
      ],
      [
        "a thrown fetch",
        () => {
          throw new TypeError("Failed to fetch");
        },
        FALLBACK_ERROR,
      ],
      [
        "a 200 whose answer is a number",
        () => jsonResponse({ answer: 7 }, 200),
        FALLBACK_ERROR,
      ],
      [
        "a 200 with no answer",
        () => jsonResponse({ intent: "company" }, 200),
        FALLBACK_ERROR,
      ],
      [
        "a 200 whose answer is blank",
        () => jsonResponse({ answer: "  " }, 200),
        FALLBACK_ERROR,
      ],
      [
        "a 200 whose body is an array",
        () => jsonResponse([{ answer: "x" }], 200),
        FALLBACK_ERROR,
      ],
      [
        "a 200 whose quick_replies is a string",
        () => jsonResponse({ answer: "Fine.", quick_replies: "x" }, 200),
        FALLBACK_ERROR,
      ],
      [
        "a 200 whose model is a number",
        () => jsonResponse({ answer: "Fine.", model: 3 }, 200),
        FALLBACK_ERROR,
      ],
    ];

    it.each(failures)(
      "rolls back %s after a valid turn and keeps it out of the next history",
      async (_label, failing, expectedError) => {
        const { bodies } = mockConcierge([
          () => wellFormed(CONTROL_ANSWER),
          failing,
          () => wellFormed("Zoning differs per site."),
        ]);
        render(<VoiceConciergeClient />);

        await sendControlTurn();
        expect(listHas(CONTROL_QUESTION)).toBe(true);

        await send(FAILED_QUESTION);
        await waitFor(() =>
          expect(screen.getByText(expectedError)).toBeTruthy(),
        );

        // The prior turns survive, the failed one is not in the list, the
        // visitor gets the text back.
        expect(listHas(CONTROL_QUESTION)).toBe(true);
        expect(listHas(CONTROL_ANSWER)).toBe(true);
        expect(listHas(FAILED_QUESTION)).toBe(false);
        expect(promptBox().value).toBe(FAILED_QUESTION);

        // Sending again carries only the turns that succeeded.
        fireEvent.click(screen.getByRole("button", { name: "Send" }));
        await waitFor(() =>
          expect(listHas("Zoning differs per site.")).toBe(true),
        );
        expect(bodies).toHaveLength(3);
        expect(bodies[2].message).toBe(FAILED_QUESTION);
        expect(bodies[2].history).toEqual([
          { role: "user", content: CONTROL_QUESTION },
          { role: "assistant", content: CONTROL_ANSWER },
        ]);
      },
    );

    it("rolls back a failed first turn to an empty conversation", async () => {
      const { bodies } = mockConcierge([
        () => jsonResponse({ error: "message too long" }, 400),
        () => wellFormed(CONTROL_ANSWER),
      ]);
      render(<VoiceConciergeClient />);

      await send(FAILED_QUESTION);
      await waitFor(() =>
        expect(screen.getByText("message too long")).toBeTruthy(),
      );
      expect(listHas(FAILED_QUESTION)).toBe(false);
      expect(promptBox().value).toBe(FAILED_QUESTION);

      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      await waitFor(() => expect(listHas(CONTROL_ANSWER)).toBe(true));
      expect(bodies[1].history).toEqual([]);
    });

    it("appends both turns on a well-formed answer and replays them (valid control)", async () => {
      const { bodies } = mockConcierge([
        () => wellFormed(CONTROL_ANSWER),
        () => wellFormed("Zoning differs per site."),
      ]);
      render(<VoiceConciergeClient />);

      await sendControlTurn();
      expect(listHas(CONTROL_QUESTION)).toBe(true);
      expect(promptBox().value).toBe("");
      expect(screen.getByText("Ask about KBLI")).toBeTruthy();

      await send(FAILED_QUESTION);
      await waitFor(() =>
        expect(listHas("Zoning differs per site.")).toBe(true),
      );
      expect(bodies[1].history).toEqual([
        { role: "user", content: CONTROL_QUESTION },
        { role: "assistant", content: CONTROL_ANSWER },
      ]);
    });

    it("limits the prompt box to the shared message length", () => {
      render(<VoiceConciergeClient />);

      expect(promptBox().maxLength).toBe(VOICE_CONCIERGE_MAX_MESSAGE_LENGTH);
      expect(promptBox().maxLength).toBe(1200);
    });

    it("sends a transcript of exactly 1200 characters", async () => {
      const { bodies } = mockConcierge([() => wellFormed(CONTROL_ANSWER)]);
      render(<VoiceConciergeClient />);

      await send("a".repeat(1200));

      await waitFor(() => expect(listHas(CONTROL_ANSWER)).toBe(true));
      expect(bodies).toHaveLength(1);
      expect(bodies[0].message).toHaveLength(1200);
    });

    it("refuses a transcript of 1201 characters without a request", async () => {
      const { bodies } = mockConcierge([]);
      render(<VoiceConciergeClient />);

      await send("a".repeat(1201));

      await waitFor(() =>
        expect(
          screen.getByText("Message too long: at most 1,200 characters."),
        ).toBeTruthy(),
      );
      expect(bodies).toHaveLength(0);
      expect(promptBox().value).toBe("a".repeat(1201));
      expect(screen.getByText(/Ask about visa, PT PMA/)).toBeTruthy();
    });

    it("keeps the earlier turns when a 1201-character transcript is refused", async () => {
      const { bodies } = mockConcierge([() => wellFormed(CONTROL_ANSWER)]);
      render(<VoiceConciergeClient />);

      await sendControlTurn();
      await send("a".repeat(1201));

      await waitFor(() =>
        expect(
          screen.getByText("Message too long: at most 1,200 characters."),
        ).toBeTruthy(),
      );
      expect(bodies).toHaveLength(1);
      expect(listHas(CONTROL_QUESTION)).toBe(true);
      expect(listHas(CONTROL_ANSWER)).toBe(true);
    });
  });
});

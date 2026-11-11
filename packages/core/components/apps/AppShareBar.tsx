"use client";

import { useEffect, useRef, useState, type FC } from "react";

export interface AppShareBarProps {
  url: string;
  title: string;
  onShare?: (channel: "copy" | "twitter" | "email") => void;
}

/** Minimal share bar: copy URL + 2 canonical channels. No third-party SDKs. */
export const AppShareBar: FC<AppShareBarProps> = ({ url, title, onShare }) => {
  const [copied, setCopied] = useState(false);
  const copiedTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (copiedTimerRef.current !== null) {
        window.clearTimeout(copiedTimerRef.current);
        copiedTimerRef.current = null;
      }
    };
  }, []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      onShare?.("copy");
      if (
        typeof navigator !== "undefined" &&
        typeof navigator.vibrate === "function"
      ) {
        navigator.vibrate(4);
      }
      if (copiedTimerRef.current !== null) {
        window.clearTimeout(copiedTimerRef.current);
      }
      // Unmounted while the clipboard write was pending: schedule nothing.
      if (!mountedRef.current) return;
      copiedTimerRef.current = window.setTimeout(() => {
        copiedTimerRef.current = null;
        setCopied(false);
      }, 1500);
    } catch {
      /* user denied clipboard */
    }
  };
  const twitter = `https://twitter.com/intent/tweet?text=${encodeURIComponent(title)}&url=${encodeURIComponent(url)}`;
  const mailto = `mailto:?subject=${encodeURIComponent(title)}&body=${encodeURIComponent(url)}`;
  return (
    <div
      aria-label="Share"
      style={{
        display: "flex",
        gap: "var(--space-2)",
        fontSize: "var(--text-sm)",
      }}
    >
      <button
        type="button"
        onClick={copy}
        style={{
          padding: "var(--space-2) var(--space-3)",
          borderRadius: 6,
          border: "1px solid var(--color-border-subtle)",
          background: "transparent",
          cursor: "pointer",
          color: "var(--color-text-primary)",
        }}
      >
        {copied ? "Copied ✓" : "Copy link"}
      </button>
      <a
        href={twitter}
        target="_blank"
        rel="noopener noreferrer"
        onClick={() => onShare?.("twitter")}
        style={{ padding: "var(--space-2) var(--space-3)" }}
      >
        Twitter / X
      </a>
      <a
        href={mailto}
        onClick={() => onShare?.("email")}
        style={{ padding: "var(--space-2) var(--space-3)" }}
      >
        Email
      </a>
    </div>
  );
};

"use client";

import React, { useRef, useState, useEffect } from "react";
import Image from "next/image";
import Link from "next/link";
import {
  ArrowRight,
  ArrowUpRight,
  CalendarCheck,
  Camera,
  CheckCircle2,
  ChevronRight,
  FileSpreadsheet,
  FolderOpen,
  HeartHandshake,
  Receipt,
  Sparkles,
  UserCheck,
  Volume2,
  VolumeX,
  Wallet,
} from "lucide-react";
import { SERIF } from "./r19";

const UNITS = [
  { name: "Board", count: 3, dot: "rgb(16, 185, 129)" },
  { name: "Setup", count: 7, dot: "rgb(59, 130, 246)" },
  { name: "Tax", count: 5, dot: "rgb(245, 158, 11)" },
  { name: "Accounting", count: 2, dot: "rgb(20, 184, 166)" },
  { name: "Marketing", count: 1, dot: "rgb(168, 85, 247)" },
  { name: "IT", count: 1, dot: "rgb(99, 102, 241)" },
] as const;

const PREVIEW_PORTRAITS = [
  { name: "Adit", src: "/static/team/adit.jpg" },
  { name: "Angel", src: "/static/team/angel.jpg" },
  { name: "Damar", src: "/static/team/damar.jpg" },
  { name: "Dea", src: "/static/team/dea.jpg" },
  { name: "Surya", src: "/static/team/surya.jpg" },
] as const;

export function TeamReelMonthlyHub() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [isMuted, setIsMuted] = useState(true);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    v.muted = isMuted;
    const playPromise = v.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(() => {
        /* Autoplay fallback in headless test environments */
      });
    }
  }, [isMuted]);

  const keepPlaying = () => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      const p = v.play();
      if (p && typeof p.catch === "function") {
        p.catch(() => {});
      }
    }
  };

  const toggleAudio = () => {
    setIsMuted((prev) => !prev);
  };

  return (
    <section
      data-testid="kita-team-reel-hero"
      aria-label="Bali Zero Team Reel and Monthly News"
      className="mb-6 flex flex-col gap-6"
    >
      {/* ── Main Stage for the Bali Zero Team Video ── */}
      <div
        className="relative overflow-hidden rounded-2xl p-5 sm:p-7 lg:p-8"
        style={{
          background:
            "radial-gradient(circle at 24% 22%, rgba(36, 78, 122, 0.94) 0%, rgba(16, 26, 42, 0.98) 54%, rgb(9, 13, 22) 100%)",
          border: "1px solid rgba(147, 197, 253, 0.26)",
          boxShadow: "0 26px 64px -22px rgba(9, 13, 22, 0.78)",
          color: "rgb(247, 244, 238)",
        }}
      >
        {/* Ambient spotlight halos behind the video */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute -left-20 top-1/2 h-[420px] w-[420px] -translate-y-1/2 rounded-full blur-3xl"
          style={{
            background:
              "radial-gradient(circle, rgba(59, 130, 246, 0.36) 0%, rgba(212, 132, 90, 0.18) 55%, transparent 75%)",
          }}
        />
        <div
          aria-hidden="true"
          className="pointer-events-none absolute -right-16 -top-16 h-[320px] w-[320px] rounded-full blur-3xl"
          style={{
            background:
              "radial-gradient(circle, rgba(16, 185, 129, 0.22) 0%, transparent 70%)",
          }}
        />

        <div className="relative z-10 grid grid-cols-1 items-center gap-7 lg:grid-cols-12">
          {/* Left / Center Pedestal: Unstoppable Team Reel */}
          <div className="flex flex-col items-center justify-center lg:col-span-5">
            <div
              className="relative overflow-hidden rounded-[24px] p-1.5"
              style={{
                background:
                  "linear-gradient(145deg, rgba(247, 244, 238, 0.34) 0%, rgba(59, 130, 246, 0.48) 50%, rgba(212, 132, 90, 0.4) 100%)",
                boxShadow:
                  "0 0 60px rgba(37, 99, 235, 0.44), 0 24px 48px rgba(0, 0, 0, 0.82)",
              }}
            >
              <div className="relative aspect-[9/16] w-[240px] sm:w-[272px] md:w-[292px] overflow-hidden rounded-[19px] bg-black">
                <video
                  ref={videoRef}
                  data-testid="continuous-team-reel-video"
                  src="/static/team/bali-zero-team-reel-33s.mp4"
                  autoPlay
                  loop
                  muted={isMuted}
                  playsInline
                  preload="auto"
                  controls={false}
                  disablePictureInPicture
                  onPause={keepPlaying}
                  onEnded={keepPlaying}
                  className="h-full w-full object-cover pointer-events-none select-none"
                />

                {/* Top badge on the reel */}
                <div
                  className="pointer-events-none absolute left-3 top-3 flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.1em] backdrop-blur-md"
                  style={{
                    background: "rgba(9, 13, 22, 0.72)",
                    border: "1px solid rgba(247, 244, 238, 0.22)",
                    color: "rgb(247, 244, 238)",
                  }}
                >
                  <span
                    className="h-2 w-2 rounded-full animate-pulse"
                    style={{ background: "rgb(16, 185, 129)" }}
                  />
                  <span>Bali Zero Team</span>
                </div>

                {/* Audio toggle button */}
                <button
                  type="button"
                  onClick={toggleAudio}
                  className="absolute bottom-3 right-3 inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-bold uppercase tracking-[0.08em] backdrop-blur-md transition-transform hover:scale-105 focus-visible:outline-none focus-visible:ring-2"
                  style={{
                    background: "rgba(9, 13, 22, 0.82)",
                    border: "1px solid rgba(247, 244, 238, 0.28)",
                    color: "rgb(247, 244, 238)",
                  }}
                >
                  {isMuted ? (
                    <>
                      <VolumeX className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>Sound Off</span>
                    </>
                  ) : (
                    <>
                      <Volume2 className="h-3.5 w-3.5" aria-hidden="true" />
                      <span>Sound On</span>
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>

          {/* Right Showcase Column */}
          <div className="flex flex-col gap-4 lg:col-span-7">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-bold uppercase tracking-[0.12em]"
                style={{
                  background: "rgba(59, 130, 246, 0.22)",
                  border: "1px solid rgba(147, 197, 253, 0.35)",
                  color: "rgb(191, 219, 254)",
                }}
              >
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                Our Team · Our Energy
              </span>
              <span
                className="inline-flex items-center rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.1em]"
                style={{
                  background: "rgba(247, 244, 238, 0.08)",
                  color: "rgba(247, 244, 238, 0.85)",
                }}
              >
                All 19 Faces of Bali Zero
              </span>
            </div>

            <h2
              className="text-[clamp(26px,3.4vw,42px)] font-black leading-[1.06] tracking-[-0.02em]"
              style={SERIF}
            >
              Look How Great We Look Together
            </h2>

            <p
              className="max-w-2xl text-[14px] leading-relaxed sm:text-[15px]"
              style={{ color: "rgba(247, 244, 238, 0.86)" }}
            >
              Here we are — all 19 of us! Your new personal photos are ready and
              waiting for you inside your own Google Drive folder, together with
              the official Bali Zero logo and simple tips on how to use them.
              Pick your favorite shot, set it on your WhatsApp, and check that
              your Google and Zoho profiles look great.
            </p>

            {/* Department pills */}
            <div className="flex flex-wrap gap-2 pt-1">
              {UNITS.map((u) => (
                <span
                  key={u.name}
                  className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-[11px] font-semibold"
                  style={{
                    background: "rgba(247, 244, 238, 0.08)",
                    border: "1px solid rgba(247, 244, 238, 0.16)",
                    color: "rgb(247, 244, 238)",
                  }}
                >
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{ background: u.dot }}
                  />
                  <span>{u.name}</span>
                  <span style={{ color: "rgba(247, 244, 238, 0.64)" }}>
                    {u.count}
                  </span>
                </span>
              ))}
            </div>

            {/* Quick Drive Path Strip inside the stage */}
            <div
              className="mt-2 flex flex-wrap items-center justify-between gap-4 rounded-xl p-4"
              style={{
                background: "rgba(247, 244, 238, 0.07)",
                border: "1px solid rgba(247, 244, 238, 0.16)",
              }}
            >
              <div className="min-w-0 flex-1">
                <p
                  className="text-[11px] font-extrabold uppercase tracking-[0.12em]"
                  style={{ color: "rgb(191, 219, 254)" }}
                >
                  Where to Find Your Photos on Google Drive
                </p>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[12px] font-bold">
                  <span
                    className="rounded-md px-2 py-0.5"
                    style={{ background: "rgba(255, 255, 255, 0.1)" }}
                  >
                    Department
                  </span>
                  <ChevronRight className="h-3.5 w-3.5 opacity-60" />
                  <span
                    className="rounded-md px-2 py-0.5"
                    style={{ background: "rgba(255, 255, 255, 0.1)" }}
                  >
                    Team
                  </span>
                  <ChevronRight className="h-3.5 w-3.5 opacity-60" />
                  <span
                    className="rounded-md px-2 py-0.5"
                    style={{ background: "rgba(255, 255, 255, 0.1)" }}
                  >
                    Your Name
                  </span>
                  <ChevronRight className="h-3.5 w-3.5 opacity-60" />
                  <span
                    className="rounded-md px-2.5 py-0.5"
                    style={{
                      background: "rgba(59, 130, 246, 0.28)",
                      border: "1px solid rgba(147, 197, 253, 0.4)",
                      color: "rgb(219, 234, 254)",
                    }}
                  >
                    Material Marketing
                  </span>
                </div>
              </div>
              <a
                href="https://drive.google.com/drive/u/1/shared-drives"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-4 py-2.5 text-[12px] font-bold transition-transform hover:scale-[1.02]"
                style={{
                  background: "rgb(29, 78, 216)",
                  color: "rgb(255, 255, 255)",
                  boxShadow: "0 10px 24px -8px rgba(59, 130, 246, 0.65)",
                }}
              >
                <FolderOpen className="h-4 w-4" aria-hidden="true" />
                <span>Open Google Drive</span>
                <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
              </a>
            </div>
          </div>
        </div>
      </div>

      {/* ── Below the Video: 3 High-Impact Feature Cards ("What's New This Month") ── */}
      <div data-testid="kita-monthly-news-grid" className="flex flex-col gap-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--bz-border)] pb-2.5">
          <div className="flex items-center gap-2.5">
            <span className="inline-flex h-2 w-2 rounded-full bg-[var(--bz-copper)]" />
            <span className="text-[11px] font-extrabold uppercase tracking-[0.14em] text-[var(--bz-copper-text)]">
              What&apos;s New This Month
            </span>
            <span className="text-[12px] font-semibold text-[var(--tx-secondary)]">
              · Fresh photos &amp; new tools made to make your day easier
            </span>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {/* ── CARD 1: Your New Photos & Brand Kit (Deep Midnight & Gold-Blue) ── */}
          <div
            data-testid="monthly-box-brand-kit"
            className="group relative flex flex-col justify-between overflow-hidden rounded-2xl p-5 sm:p-6 transition-all duration-200 hover:-translate-y-1"
            style={{
              background:
                "linear-gradient(155deg, rgb(15, 23, 42) 0%, rgb(23, 37, 68) 55%, rgb(13, 20, 36) 100%)",
              border: "1px solid rgba(147, 197, 253, 0.32)",
              boxShadow: "0 20px 42px -18px rgba(15, 23, 42, 0.68)",
              color: "rgb(247, 244, 238)",
            }}
          >
            {/* Top accent bar */}
            <div
              aria-hidden="true"
              className="absolute inset-x-0 top-0 h-1.5"
              style={{
                background:
                  "linear-gradient(90deg, rgb(59, 130, 246) 0%, rgb(251, 191, 36) 100%)",
              }}
            />
            {/* Ambient corner glow */}
            <div
              aria-hidden="true"
              className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full blur-2xl"
              style={{
                background: "rgba(59, 130, 246, 0.25)",
              }}
            />

            <div className="relative z-10 flex flex-col gap-4">
              {/* Top badge row */}
              <div className="flex items-center justify-between gap-2">
                <span
                  className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-extrabold uppercase tracking-[0.1em]"
                  style={{
                    background: "rgba(59, 130, 246, 0.24)",
                    border: "1px solid rgba(147, 197, 253, 0.4)",
                    color: "rgb(191, 219, 254)",
                  }}
                >
                  <Camera className="h-3.5 w-3.5" aria-hidden="true" />
                  Your New Photos
                </span>
                <span
                  className="rounded-full px-2.5 py-0.5 text-[11px] font-bold uppercase tracking-[0.08em]"
                  style={{
                    background: "rgba(251, 191, 36, 0.18)",
                    color: "rgb(253, 224, 71)",
                  }}
                >
                  For Everyone
                </span>
              </div>

              {/* Visual Avatar Stack + Platform Pills */}
              <div
                className="flex flex-wrap items-center justify-between gap-3 rounded-xl p-3"
                style={{
                  background: "rgba(255, 255, 255, 0.06)",
                  border: "1px solid rgba(255, 255, 255, 0.12)",
                }}
              >
                <div className="flex items-center -space-x-2.5">
                  {PREVIEW_PORTRAITS.map((p) => (
                    <span
                      key={p.name}
                      className="relative inline-block h-9 w-9 overflow-hidden rounded-full ring-2"
                      style={{
                        borderColor: "rgb(15, 23, 42)",
                      }}
                    >
                      <Image
                        src={p.src}
                        alt={p.name}
                        fill
                        sizes="40px"
                        className="object-cover"
                      />
                    </span>
                  ))}
                  <span
                    className="relative inline-flex h-9 w-9 items-center justify-center rounded-full text-[11px] font-extrabold ring-2"
                    style={{
                      background: "rgb(37, 99, 235)",
                      color: "rgb(255, 255, 255)",
                    }}
                  >
                    +14
                  </span>
                </div>

                <div className="flex flex-wrap items-center gap-1">
                  {["WhatsApp", "Google", "Zoho"].map((app) => (
                    <span
                      key={app}
                      className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-bold"
                      style={{
                        background: "rgba(16, 185, 129, 0.2)",
                        color: "rgb(167, 243, 208)",
                      }}
                    >
                      <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
                      {app}
                    </span>
                  ))}
                </div>
              </div>

              <h3
                className="text-[20px] font-black leading-snug tracking-[-0.01em]"
                style={SERIF}
              >
                Put Your New Photo on WhatsApp, Google &amp; Zoho
              </h3>

              <p
                className="text-[13px] leading-relaxed"
                style={{ color: "rgba(247, 244, 238, 0.84)" }}
              >
                Open your folder inside{" "}
                <strong className="font-bold text-white">
                  Department → Team → Your Name → Material Marketing
                </strong>
                . You&apos;ll find all your new photos, the official Bali Zero
                logo, and our brand guide. Pick the photo you love most for{" "}
                <strong className="font-bold text-white">WhatsApp</strong> and
                double-check that your{" "}
                <strong className="font-bold text-white">Google</strong> and{" "}
                <strong className="font-bold text-white">Zoho</strong> profiles
                are complete!
              </p>
            </div>

            <div
              className="relative z-10 mt-5 flex flex-wrap items-center justify-between gap-2 border-t pt-4"
              style={{ borderColor: "rgba(255, 255, 255, 0.14)" }}
            >
              <a
                href="https://drive.google.com/drive/u/1/shared-drives"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-xl px-3.5 py-2 text-[12px] font-extrabold transition-transform hover:scale-[1.02]"
                style={{
                  background: "rgb(29, 78, 216)",
                  color: "rgb(255, 255, 255)",
                }}
              >
                <span>Open Material Marketing</span>
                <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
              </a>
              <Link
                href="/team"
                className="inline-flex items-center gap-1 rounded-xl px-3 py-2 text-[12px] font-bold transition-colors"
                style={{
                  background: "rgba(255, 255, 255, 0.1)",
                  color: "rgb(247, 244, 238)",
                }}
              >
                <span>See All Team</span>
              </Link>
            </div>
          </div>

          {/* ── CARD 2: New Invoices in Kita (Deep Forest & Emerald) ── */}
          <Link
            href="/clients"
            data-testid="monthly-box-invoices"
            className="group relative flex flex-col justify-between overflow-hidden rounded-2xl p-5 sm:p-6 transition-all duration-200 hover:-translate-y-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--bz-copper)]"
            style={{
              background:
                "linear-gradient(155deg, rgb(11, 36, 31) 0%, rgb(18, 56, 47) 55%, rgb(9, 28, 24) 100%)",
              border: "1px solid rgba(52, 211, 153, 0.34)",
              boxShadow: "0 20px 42px -18px rgba(11, 36, 31, 0.68)",
              color: "rgb(247, 244, 238)",
            }}
          >
            {/* Top accent bar */}
            <div
              aria-hidden="true"
              className="absolute inset-x-0 top-0 h-1.5"
              style={{
                background:
                  "linear-gradient(90deg, rgb(16, 185, 129) 0%, rgb(52, 211, 153) 100%)",
              }}
            />
            {/* Ambient corner glow */}
            <div
              aria-hidden="true"
              className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full blur-2xl"
              style={{
                background: "rgba(16, 185, 129, 0.24)",
              }}
            />

            <div className="relative z-10 flex flex-col gap-4">
              <div className="flex items-center justify-between gap-2">
                <span
                  className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-extrabold uppercase tracking-[0.1em]"
                  style={{
                    background: "rgba(16, 185, 129, 0.24)",
                    border: "1px solid rgba(110, 231, 183, 0.38)",
                    color: "rgb(167, 243, 208)",
                  }}
                >
                  <FileSpreadsheet className="h-3.5 w-3.5" aria-hidden="true" />
                  New Invoices
                </span>
                <span
                  className="rounded-full px-2.5 py-0.5 text-[11px] font-bold uppercase tracking-[0.08em]"
                  style={{
                    background: "rgba(16, 185, 129, 0.22)",
                    color: "rgb(110, 231, 183)",
                  }}
                >
                  Live Now
                </span>
              </div>

              {/* Visual 3-Step Flow Box */}
              <div
                className="flex flex-wrap items-center justify-between gap-1.5 rounded-xl p-3 text-[11px] font-bold"
                style={{
                  background: "rgba(255, 255, 255, 0.06)",
                  border: "1px solid rgba(255, 255, 255, 0.12)",
                }}
              >
                <span
                  className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1"
                  style={{
                    background: "rgba(255, 255, 255, 0.1)",
                    color: "rgb(247, 244, 238)",
                  }}
                >
                  1. Sending Invoice
                </span>
                <ArrowRight className="h-3.5 w-3.5 opacity-60" />
                <span
                  className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1"
                  style={{
                    background: "rgba(16, 185, 129, 0.26)",
                    color: "rgb(167, 243, 208)",
                  }}
                >
                  <Receipt className="h-3 w-3" aria-hidden="true" />
                  2. PDF Created
                </span>
                <ArrowRight className="h-3.5 w-3.5 opacity-60" />
                <span
                  className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1"
                  style={{
                    background: "rgba(251, 191, 36, 0.2)",
                    color: "rgb(253, 224, 71)",
                  }}
                >
                  3. Ready for Asya
                </span>
              </div>

              <h3
                className="text-[20px] font-black leading-snug tracking-[-0.01em]"
                style={SERIF}
              >
                Invoices Now Made Automatically from Your Cases
              </h3>

              <p
                className="text-[13px] leading-relaxed"
                style={{ color: "rgba(247, 244, 238, 0.84)" }}
              >
                No more writing invoices by hand! When you move a client&apos;s
                process to{" "}
                <strong className="font-bold text-white">
                  Sending Invoice
                </strong>
                , Kita prepares the official Bali Zero PDF invoice automatically
                with the right company and bank account, ready for the client
                and for Asya in Accounting.
              </p>
            </div>

            <div
              className="relative z-10 mt-5 flex items-center justify-between border-t pt-4"
              style={{ borderColor: "rgba(255, 255, 255, 0.14)" }}
            >
              <span
                className="text-[13px] font-extrabold group-hover:underline"
                style={{ color: "rgb(167, 243, 208)" }}
              >
                Open Client Cases &amp; Invoices
              </span>
              <span
                className="inline-flex h-8 w-8 items-center justify-center rounded-full transition-transform group-hover:translate-x-1"
                style={{
                  background: "rgb(16, 185, 129)",
                  color: "rgb(9, 28, 24)",
                }}
              >
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </span>
            </div>
          </Link>

          {/* ── CARD 3: Your Personal Space in Kita (Warm Espresso & Copper-Gold) ── */}
          <Link
            href="/hr/me"
            data-testid="monthly-box-hr-portal"
            className="group relative flex flex-col justify-between overflow-hidden rounded-2xl p-5 sm:p-6 transition-all duration-200 hover:-translate-y-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--bz-copper)]"
            style={{
              background:
                "linear-gradient(155deg, rgb(46, 22, 18) 0%, rgb(72, 33, 25) 55%, rgb(32, 15, 12) 100%)",
              border: "1px solid rgba(235, 151, 118, 0.42)",
              boxShadow: "0 20px 42px -18px rgba(46, 22, 18, 0.68)",
              color: "rgb(247, 244, 238)",
            }}
          >
            {/* Top accent bar */}
            <div
              aria-hidden="true"
              className="absolute inset-x-0 top-0 h-1.5"
              style={{
                background:
                  "linear-gradient(90deg, rgb(212, 132, 90) 0%, rgb(251, 191, 36) 100%)",
              }}
            />
            {/* Ambient corner glow */}
            <div
              aria-hidden="true"
              className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full blur-2xl"
              style={{
                background: "rgba(212, 132, 90, 0.26)",
              }}
            />

            <div className="relative z-10 flex flex-col gap-4">
              <div className="flex items-center justify-between gap-2">
                <span
                  className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-extrabold uppercase tracking-[0.1em]"
                  style={{
                    background: "rgba(212, 132, 90, 0.26)",
                    border: "1px solid rgba(235, 151, 118, 0.45)",
                    color: "rgb(254, 215, 170)",
                  }}
                >
                  <UserCheck className="h-3.5 w-3.5" aria-hidden="true" />
                  Your Personal Space
                </span>
                <span
                  className="rounded-full px-2.5 py-0.5 text-[11px] font-bold uppercase tracking-[0.08em]"
                  style={{
                    background: "rgba(251, 191, 36, 0.2)",
                    color: "rgb(253, 224, 71)",
                  }}
                >
                  Just for You
                </span>
              </div>

              {/* Visual 2x2 Feature Grid */}
              <div
                className="grid grid-cols-2 gap-2 rounded-xl p-2.5 text-[11px] font-bold"
                style={{
                  background: "rgba(255, 255, 255, 0.06)",
                  border: "1px solid rgba(255, 255, 255, 0.12)",
                }}
              >
                <span
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5"
                  style={{
                    background: "rgba(255, 255, 255, 0.08)",
                    color: "rgb(254, 215, 170)",
                  }}
                >
                  <Wallet className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  <span className="truncate">Payslips &amp; Bonus</span>
                </span>
                <span
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5"
                  style={{
                    background: "rgba(255, 255, 255, 0.08)",
                    color: "rgb(254, 215, 170)",
                  }}
                >
                  <CalendarCheck
                    className="h-3.5 w-3.5 shrink-0"
                    aria-hidden="true"
                  />
                  <span className="truncate">Request Leave</span>
                </span>
                <span
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5"
                  style={{
                    background: "rgba(255, 255, 255, 0.08)",
                    color: "rgb(254, 215, 170)",
                  }}
                >
                  <CheckCircle2
                    className="h-3.5 w-3.5 shrink-0"
                    aria-hidden="true"
                  />
                  <span className="truncate">Attendance</span>
                </span>
                <span
                  className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5"
                  style={{
                    background: "rgba(255, 255, 255, 0.08)",
                    color: "rgb(254, 215, 170)",
                  }}
                >
                  <HeartHandshake
                    className="h-3.5 w-3.5 shrink-0"
                    aria-hidden="true"
                  />
                  <span className="truncate">My Profile</span>
                </span>
              </div>

              <h3
                className="text-[20px] font-black leading-snug tracking-[-0.01em]"
                style={SERIF}
              >
                Click Here to Enter Your Personal HR Page
              </h3>

              <p
                className="text-[13px] leading-relaxed"
                style={{ color: "rgba(247, 244, 238, 0.84)" }}
              >
                Your own private corner in Kita is now open! Click anywhere on
                this card to go straight to your personal page — check your
                monthly payslips and bonuses, ask for days off, and see your
                attendance in one click.
              </p>
            </div>

            <div
              className="relative z-10 mt-5 flex items-center justify-between border-t pt-4"
              style={{ borderColor: "rgba(255, 255, 255, 0.14)" }}
            >
              <span
                className="text-[13px] font-extrabold group-hover:underline"
                style={{ color: "rgb(254, 215, 170)" }}
              >
                Go to My Personal Page
              </span>
              <span
                className="inline-flex h-8 w-8 items-center justify-center rounded-full transition-transform group-hover:translate-x-1"
                style={{
                  background: "rgb(212, 132, 90)",
                  color: "rgb(32, 15, 12)",
                }}
              >
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </span>
            </div>
          </Link>
        </div>
      </div>
    </section>
  );
}

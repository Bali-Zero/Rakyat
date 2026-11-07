"use client";

import React from "react";
import { MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SERIF } from "@/components/workspace/r19/tokens";
import { CardArt } from "./cardKit";
import { PortalAccess } from "./PortalAccess";
import { PortalMessages } from "./PortalMessages";

/**
 * The branded vault: my.balizero.com, the client's own portal, as the
 * flagship card of the Overview. A dark card on the workspace rail tokens
 * (ink surface, light text) with a copper hairline OUTLINE (never a fill) and
 * the Bali Zero rings at the right of the header zone. Access and messages sit
 * below it on light panels, so their own text keeps theme contrast. It
 * composes the two live panels rather than copying their logic, so the invite
 * and send flows stay the ones already tested.
 */
export function VaultCard({
  clientId,
  clientName,
  clientEmail,
}: {
  clientId: number;
  clientName: string;
  clientEmail?: string | null;
}) {
  const openMessages = () => {
    const thread = document.getElementById("portal-messages");
    thread?.scrollIntoView({ block: "center", behavior: "smooth" });
    thread?.querySelector("input")?.focus({ preventScroll: true });
  };

  return (
    <section
      aria-label="Client portal vault"
      data-testid="vault-card"
      className="relative overflow-hidden rounded-xl border border-[var(--nav-rule)] bg-[var(--bz-kita-surface-navy,var(--nav-rail-bg))] p-6 text-[var(--nav-fg)]"
    >
      {/* Header zone. Mobile: a 2-column grid (text 1fr, cube 72px) with the
          promise line full width on its own row below; md and up: the same
          three children as a fixed-height text-left / cube-right zone. The
          title never sits under the image. */}
      <header
        data-testid="vault-card-header"
        className="grid grid-cols-[minmax(0,1fr)_120px] items-start gap-x-3 gap-y-2 md:h-[176px] md:grid-cols-[minmax(0,1fr)_200px] md:grid-rows-[auto_1fr] md:gap-x-4 md:overflow-hidden"
      >
        <div className="min-w-0">
          <p className="text-[12px] font-semibold text-[var(--nav-fg-muted)]">
            Client portal
          </p>
          <h2
            className="mt-1 text-[clamp(22px,6.5vw,32px)] leading-[1.05] tracking-[-0.03em] md:text-[34px]"
            style={SERIF}
            data-testid="vault-wordmark"
          >
            my.balizero.com
          </h2>
        </div>
        {/* Gold rings around the round Bali Zero disc: bleeds past the card's
            top/right edge (the card clips it) while the disc stays inside. */}
        <CardArt
          name="vault-rings"
          alt="Bali Zero"
          size={200}
          className="-mr-4 -mt-4 h-[120px] w-[120px] object-center md:-mr-6 md:-mt-6 md:row-span-2 md:h-[200px] md:w-[200px]"
        />
        <p
          className="col-span-2 max-w-[56ch] text-[13px] leading-[1.5] text-[var(--nav-fg-muted)] md:col-span-1 md:row-start-2"
          data-testid="vault-promise"
        >
          The client&apos;s private vault: documents, permits and messages in
          one place.
        </p>
      </header>
      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div className="flex flex-col gap-3 rounded-lg border border-[var(--bz-border)] bg-[var(--bz-card)] p-4 text-[var(--tx-pure)]">
          <PortalAccess
            embedded
            clientId={clientId}
            clientName={clientName}
            clientEmail={clientEmail}
          />
          <div>
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5 border-[var(--line-control)] text-[var(--tx-pure)]"
              onClick={openMessages}
            >
              <MessageSquare className="h-4 w-4" aria-hidden="true" />
              Open messages
            </Button>
          </div>
        </div>
        <PortalMessages embedded clientId={clientId} clientName={clientName} />
      </div>
    </section>
  );
}

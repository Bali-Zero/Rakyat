"use client";

import React, { useState } from "react";
import {
  Check,
  ChevronRight,
  Copy,
  Edit2,
  MessageCircle,
  Phone,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ClientProfile } from "@/lib/api/crm/crm.types";
import { StatePill } from "@/components/workspace/r19";
import { FOCUS, MICRO_LABEL } from "@/components/workspace/r19/tokens";
import { formatPhoneNumber, isBirthdayToday } from "./utils";
import { ageYears, humanize, type ContactFact } from "./overview-brief";
import { lastContactLabel } from "../../client-row-model";
import { CardFrame, Fact, Facts, NOT_RECORDED } from "./cardKit";
import { LogContact } from "./LogContact";

/** wa.me link for a stored phone: a leading 0 is an Indonesian local number. */
export function whatsappUrl(phone: string): string | null {
  const digits = phone.replace(/\D/g, "");
  if (!digits) return null;
  return `https://wa.me/${digits.startsWith("0") ? "62" + digits.slice(1) : digits}`;
}

/**
 * Who the client is, with every way to reach them one click away. Neutral
 * (ink) card: the other two Overview cards carry the slate and forest tints.
 */
export function ClientInfoCard({
  client,
  now,
  contact,
  formatDate,
  onEditClick,
  onDeleteClick,
  canDeleteClient,
  isDeletingClient,
  clientId,
  viewerEmail = null,
  onLogged = () => {},
}: {
  client: ClientProfile["client"];
  now: number;
  contact: ContactFact;
  formatDate: (d: string) => string;
  onEditClick: () => void;
  onDeleteClick: () => void;
  canDeleteClient: boolean;
  isDeletingClient: boolean;
  clientId?: number;
  /** Who logs a contact; null while unknown, which disables "Log contact". */
  viewerEmail?: string | null;
  onLogged?: () => Promise<void> | void;
}) {
  const [copiedField, setCopiedField] = useState<string | null>(null);

  const copyToClipboard = (value: string, field: string) => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopiedField(field);
      setTimeout(() => setCopiedField(null), 2000);
    });
  };

  const copyButton = (
    value: string | undefined,
    field: string,
    label: string,
  ) =>
    value ? (
      <button
        type="button"
        onClick={() => copyToClipboard(value, field)}
        className={`ml-1.5 inline-flex min-h-8 min-w-8 items-center justify-center rounded align-middle text-[var(--tx-secondary)] opacity-70 transition-opacity hover:opacity-100 ${FOCUS}`}
        title={`Copy ${label}`}
        aria-label={`Copy ${label}`}
      >
        {copiedField === field ? (
          <Check className="h-3.5 w-3.5 text-[var(--state-success)]" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </button>
    ) : null;

  const age = ageYears(client.date_of_birth, now);
  const gender =
    client.gender === "M" ? "Male" : client.gender === "F" ? "Female" : null;
  const taxId = client.npwp || client.tax_id;
  const wa = client.phone ? whatsappUrl(client.phone) : null;

  return (
    <CardFrame
      tone="neutral"
      kicker="Contact"
      title="Client info"
      art="client-info"
      testId="client-info-card"
      actions={
        <>
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5 border-[var(--line-control)]"
            onClick={onEditClick}
            aria-label="Edit client info"
          >
            <Edit2 className="h-3.5 w-3.5" aria-hidden="true" />
            Edit
          </Button>
          {canDeleteClient ? (
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto h-8 w-8 p-0 text-[var(--tx-secondary)] hover:text-[var(--tx-pure)]"
              onClick={onDeleteClick}
              disabled={isDeletingClient}
              aria-label="Delete client"
              title="Delete client"
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" />
            </Button>
          ) : null}
        </>
      }
    >
      <Facts>
        <Fact label="Full name" wide>
          <span className="text-[16px] font-semibold">{client.full_name}</span>
        </Fact>
        <Fact label="Email" wide>
          {client.email ? (
            <>
              <a
                href={`mailto:${client.email}`}
                className="underline decoration-[var(--bz-border)] underline-offset-4 hover:decoration-[var(--tx-pure)]"
              >
                {client.email}
              </a>
              {copyButton(client.email, "email", "email")}
            </>
          ) : (
            NOT_RECORDED
          )}
        </Fact>
        <Fact label="Phone" wide>
          {client.phone ? (
            <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
              <span style={{ fontVariantNumeric: "tabular-nums" }}>
                {formatPhoneNumber(client.phone)}
              </span>
              <a
                href={`tel:${client.phone}`}
                className="inline-flex items-center gap-1 text-[12px] font-semibold text-[var(--state-info)] hover:underline"
                title={`Call ${client.phone}`}
              >
                <Phone className="h-3 w-3" aria-hidden="true" />
                Call
              </a>
              {wa ? (
                <a
                  href={wa}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-[12px] font-semibold text-[var(--state-info)] hover:underline"
                  title="Open WhatsApp chat"
                >
                  <MessageCircle className="h-3 w-3" aria-hidden="true" />
                  WhatsApp
                </a>
              ) : null}
              {copyButton(client.phone, "phone", "phone")}
            </span>
          ) : (
            NOT_RECORDED
          )}
        </Fact>
        <Fact label="Nationality">{client.nationality || NOT_RECORDED}</Fact>
        <Fact label="Gender">{gender || NOT_RECORDED}</Fact>
        <Fact label="Age and birth date" wide>
          {client.date_of_birth ? (
            <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
              {age !== null ? <span>{age} y</span> : null}
              <span className="text-[var(--tx-secondary)]">
                {age !== null ? "· " : ""}born{" "}
                {formatDate(client.date_of_birth)}
              </span>
              {isBirthdayToday(client.date_of_birth) ? (
                <StatePill tone="ours" label="Birthday today" />
              ) : null}
            </span>
          ) : (
            NOT_RECORDED
          )}
        </Fact>
        <Fact label="Address" wide>
          {client.address || NOT_RECORDED}
        </Fact>
        <Fact label="Last contact" wide testId="client-last-contact">
          {contact.days === null ? (
            <span className="text-[var(--state-warning)]">
              No interaction logged yet
            </span>
          ) : (
            <span>
              {lastContactLabel(contact.days)}
              {contact.channel ? (
                <span className="text-[var(--tx-secondary)]">
                  {" "}
                  · {contact.channel}
                </span>
              ) : null}
              {contact.days >= 30 ? (
                <span className="font-semibold text-[var(--state-warning)]">
                  {" "}
                  · silent for {contact.days} days
                </span>
              ) : null}
            </span>
          )}
          {clientId !== undefined && (
            <LogContact
              clientId={clientId}
              teamMember={viewerEmail}
              onLogged={onLogged}
            />
          )}
        </Fact>
      </Facts>
      <details className="group mt-3" data-testid="more-details">
        <summary
          className={`flex min-h-8 cursor-pointer list-none items-center gap-1.5 text-[12px] font-medium text-[var(--tx-secondary)] hover:text-[var(--tx-pure)] [&::-webkit-details-marker]:hidden ${FOCUS}`}
        >
          <ChevronRight
            aria-hidden="true"
            className="h-3 w-3 transition-transform group-open:rotate-90"
          />
          More details
        </summary>
        <dl className="grid grid-cols-2 gap-x-5 gap-y-3.5 pt-2">
          {taxId ? (
            <Fact label="NPWP" wide>
              <span style={{ fontVariantNumeric: "tabular-nums" }}>
                {taxId}
              </span>
              {copyButton(taxId, "npwp", "NPWP")}
            </Fact>
          ) : null}
          <Fact label="Client since" wide>
            {formatDate(client.first_contact_date || client.created_at)}
            {client.lead_source ? (
              <span className="text-[var(--tx-secondary)]">
                {" "}
                · via {humanize(client.lead_source)}
              </span>
            ) : null}
          </Fact>
        </dl>
        {client.notes ? (
          <div className="pt-3">
            <p className={MICRO_LABEL}>System log</p>
            <p className="mt-2 max-h-64 overflow-y-auto whitespace-pre-line font-mono text-xs leading-relaxed text-[var(--tx-secondary)]">
              {client.notes}
            </p>
          </div>
        ) : null}
      </details>
    </CardFrame>
  );
}

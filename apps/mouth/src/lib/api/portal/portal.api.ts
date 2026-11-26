/**
 * Portal API Client
 * Handles all client portal API calls
 */

import type { ApiClientBase } from "../client";
import { measurePortalAction } from "@/lib/portal-analytics";
import type {
  PortalDashboard,
  VisaInfo,
  PortalCompany,
  TaxOverview,
  TaxObligation,
  PortalDocument,
  MessagesResponse,
  PortalMessage,
  SendMessageRequest,
  PortalPreferences,
  PortalProfile,
  PortalProfileIdentity,
  UpdateProfileRequest,
  InviteValidationResponse,
  CompleteRegistrationRequest,
  RegistrationResponse,
  PortalApiResponse,
  LKPMDraftSummary,
  LKPMDraft,
  ProcessTimeline,
  DriveFilesResponse,
  BillingResponse,
  PaymentProof,
  NotificationsResponse,
  DashboardSummary,
  PortalMatter,
  PortalMatterDetail,
} from "./portal.types";
import type { TimelineResponse } from "../types/timeline.types";

type RawPortalMessage = {
  id: number | string;
  content: string;
  direction?: "client_to_team" | "team_to_client";
  from_team?: boolean;
  sentBy?: string;
  sent_by?: string;
  subject?: string | null;
  practiceId?: number | null;
  practice_id?: number | null;
  practiceName?: string | null;
  practice_name?: string | null;
  createdAt?: string;
  created_at?: string;
  readAt?: string | null;
  read_at?: string | null;
  is_read?: boolean;
  is_system?: boolean;
};

type RawMessagesResponse = {
  messages?: RawPortalMessage[];
  total?: number;
  unreadCount?: number;
  unread_count?: number;
};

// Status-update messages stored before the backend fix (PR 8486) carry the
// internal practice code, e.g. "Status update: visa_d12_business_1yr". Stored
// rows are not rewritten, so the reader names the service: practice_name from
// the API first, else the code without underscores (same rule as the backend).
// Only that exact template is touched.
const LEGACY_STATUS_SUBJECT = /^Status update: ([a-z0-9]+(?:_[a-z0-9]+)+)$/;

function nameLegacyStatusText(
  subject: string | undefined,
  content: string,
  practiceName: string | null | undefined,
): { subject: string | undefined; content: string } {
  const code = subject?.match(LEGACY_STATUS_SUBJECT)?.[1];
  if (!code) return { subject, content };
  const display = practiceName?.trim() || code.replace(/_/g, " ");
  return {
    subject: `Status update: ${display}`,
    content: content.replace(`Your ${code} status`, `Your ${display} status`),
  };
}

function normalizePortalMessage(message: RawPortalMessage): PortalMessage {
  const practiceName = message.practiceName ?? message.practice_name;
  const text = nameLegacyStatusText(
    message.subject ?? undefined,
    message.content,
    practiceName,
  );
  return {
    id: String(message.id),
    content: text.content,
    direction:
      message.direction ??
      (message.from_team === true ? "team_to_client" : "client_to_team"),
    sentBy: message.sentBy ?? message.sent_by ?? "",
    subject: text.subject,
    practiceId: message.practiceId ?? message.practice_id ?? undefined,
    practiceName: practiceName ?? undefined,
    createdAt: message.createdAt ?? message.created_at ?? "",
    readAt:
      message.readAt ??
      message.read_at ??
      (message.is_read === true
        ? (message.createdAt ?? message.created_at)
        : undefined) ??
      undefined,
    isSystem: message.is_system === true ? true : undefined,
  };
}

function normalizeMessagesResponse(
  response: RawMessagesResponse,
): MessagesResponse {
  return {
    messages: (response.messages ?? []).map(normalizePortalMessage),
    total: response.total ?? 0,
    unreadCount: response.unreadCount ?? response.unread_count ?? 0,
  };
}

/** The stored profile plus the contact fields filed as change requests. */
export type ProfileUpdateResult = PortalProfile & {
  contactChangeRequested: string[];
};

export class PortalApi {
  constructor(private client: ApiClientBase) {}

  // ============================================================================
  // Dashboard
  // ============================================================================

  async getDashboard(): Promise<PortalDashboard> {
    const response = await this.client.request<
      PortalApiResponse<PortalDashboard>
    >("/api/portal/dashboard", { method: "GET" });
    return response.data!;
  }

  async getDashboardSummary(): Promise<DashboardSummary> {
    return this.client.request<DashboardSummary>(
      "/api/portal/dashboard/summary",
      { method: "GET" },
    );
  }

  async listMatters(): Promise<{ matters: PortalMatter[] }> {
    return this.client.request<{ matters: PortalMatter[] }>(
      "/api/portal/matters",
      { method: "GET" },
    );
  }

  async getMatterDetail(matterId: number): Promise<PortalMatterDetail> {
    const response = await this.client.request<{ matter: PortalMatterDetail }>(
      `/api/portal/matters/${matterId}`,
      { method: "GET" },
    );
    return response.matter;
  }

  async getTimeline(limit: number = 50): Promise<TimelineResponse> {
    const response = await this.client.request<
      PortalApiResponse<TimelineResponse>
    >(`/api/portal/timeline?limit=${limit}`, { method: "GET" });
    return response.data!;
  }

  // ============================================================================
  // Profile
  // ============================================================================

  async getProfile(): Promise<PortalProfile> {
    const response = await this.client.request<PortalApiResponse<any>>(
      "/api/portal/profile",
      { method: "GET" },
    );

    // Map snake_case backend response to camelCase frontend types
    const data = response.data!;
    return {
      id: data.id,
      fullName: data.full_name,
      email: data.email,
      phone: data.phone,
      whatsapp: data.whatsapp,
      nationality: data.nationality,
      passportNumber: data.passport_number,
      passportExpiry: data.passport_expiry,
      dateOfBirth: data.date_of_birth,
      gender: data.gender,
      address: data.address,
      memberSince: data.member_since,
      identityMasked: data.identity_masked,
      birthdayToday: data.birthday_today,
      assignedTo: data.assigned_to
        ? {
            email: data.assigned_to.email,
            name: data.assigned_to.name,
            avatarUrl: data.assigned_to.avatar_url,
          }
        : undefined,
    };
  }

  /**
   * Full passport number + date of birth for the signed-in client only. The
   * backend refuses impersonation sessions and audit-logs every reveal.
   */
  async revealProfileIdentity(): Promise<PortalProfileIdentity> {
    const response = await this.client.request<
      PortalApiResponse<{
        passport_number: string | null;
        date_of_birth: string | null;
      }>
    >("/api/portal/profile/reveal", { method: "POST" });
    const d = response.data!;
    return {
      passportNumber: d.passport_number ?? undefined,
      dateOfBirth: d.date_of_birth ?? undefined,
    };
  }

  // ============================================================================
  // Profile Update
  // ============================================================================

  async updateProfile(data: UpdateProfileRequest): Promise<ProfileUpdateResult> {
    const response = await this.client.request<
      PortalApiResponse<any> & { contact_change_requested?: string[] }
    >(
      "/api/portal/profile",
      { method: "PATCH", body: JSON.stringify(data) },
    );
    const d = response.data!;
    return {
      id: d.id,
      fullName: d.full_name,
      email: d.email,
      phone: d.phone,
      whatsapp: d.whatsapp,
      nationality: d.nationality,
      passportNumber: d.passport_number,
      passportExpiry: d.passport_expiry,
      dateOfBirth: d.date_of_birth,
      gender: d.gender,
      address: d.address,
      memberSince: d.member_since,
      identityMasked: d.identity_masked,
      birthdayToday: d.birthday_today,
      assignedTo: d.assigned_to
        ? {
            email: d.assigned_to.email,
            name: d.assigned_to.name,
            avatarUrl: d.assigned_to.avatar_url,
          }
        : undefined,
      contactChangeRequested: response.contact_change_requested ?? [],
    };
  }

  // ============================================================================
  // Visa & Immigration
  // ============================================================================

  async getVisaStatus(): Promise<VisaInfo> {
    // Backend may return {success, data: {current, history, documents}} or
    // the legacy {summary, current_visa, history} shape. Accept both.
    const response = await this.client.request<any>("/api/portal/visa", {
      method: "GET",
    });

    const raw = response.data ?? response;
    const visa = raw.current ?? raw.current_visa ?? null;

    return {
      current: visa
        ? {
            type: visa.visa_type ?? visa.type ?? "",
            status: visa.status ?? "expired",
            issueDate: visa.issue_date ?? visa.issueDate ?? "",
            expiryDate: visa.expiry_date ?? visa.expiryDate ?? "",
            // null = no expiry on record: the page shows no countdown
            // instead of "Expired 0d ago".
            daysRemaining:
              visa.daysRemaining === null
                ? null
                : (raw.summary?.days_until_expiry ??
                  visa.days_remaining ??
                  visa.daysRemaining ??
                  0),
            permitNumber:
              visa.visa_number ?? visa.permit_number ?? visa.permitNumber ?? "",
            sponsor: visa.sponsor_name ?? visa.sponsor ?? "",
          }
        : null,
      history: (raw.history ?? []).map((h: Record<string, unknown>) => ({
        id: String(h.id ?? ""),
        type: (h.visa_type as string) ?? (h.type as string) ?? "",
        period:
          typeof h.period === "string" && h.period.trim().length > 0
            ? h.period
            : `${h.issue_date ?? h.issueDate ?? ""} — ${h.expiry_date ?? h.expiryDate ?? ""}`,
        status:
          h.status === "completed" || h.status === "active"
            ? "completed"
            : "expired",
      })),
      documents: (raw.documents ?? []).map((d: Record<string, unknown>) => ({
        id: String(d.id ?? ""),
        name: (d.name as string) ?? "",
        type: (d.type as string) ?? "",
        category: (d.category as string) ?? "",
        status: (d.status as string) ?? "",
        uploadDate: (d.uploadDate as string) ?? (d.upload_date as string) ?? "",
        expiryDate: (d.expiryDate as string) ?? (d.expiry_date as string) ?? "",
        size: (d.size as string) ?? "",
        downloadUrl:
          (d.downloadUrl as string) ?? (d.download_url as string) ?? "",
      })),
    };
  }

  // ============================================================================
  // Companies
  // ============================================================================

  async getCompanies(): Promise<PortalCompany[]> {
    const response = await this.client.request<
      PortalApiResponse<PortalCompany[]>
    >("/api/portal/companies", { method: "GET" });
    return response.data!;
  }

  async getCompanyDetail(companyId: number): Promise<PortalCompany> {
    const response = await this.client.request<
      PortalApiResponse<PortalCompany>
    >(`/api/portal/company/${companyId}`, { method: "GET" });
    return response.data!;
  }

  async setPrimaryCompany(companyId: number): Promise<void> {
    await this.client.request<PortalApiResponse<void>>(
      `/api/portal/company/${companyId}/select`,
      {
        method: "POST",
      },
    );
  }

  // ============================================================================
  // Taxes
  // ============================================================================

  async getTaxOverview(): Promise<TaxOverview> {
    const response = await this.client.request<
      PortalApiResponse<Record<string, unknown>>
    >("/api/portal/taxes", { method: "GET" });
    const raw = (response.data ?? {}) as Record<string, unknown>;
    const summary = (raw.summary ?? {}) as Record<string, unknown>;
    const obligations = (raw.obligations ?? []) as Record<string, unknown>[];
    // Backend currently emits camelCase keys (totalDue, nextDeadline,
    // daysToDeadline, dueDate, type, period). Older builds used snake_case
    // (total_due, next_deadline…). Accept either so a silent backend schema
    // drift doesn't wipe out the UI again.
    const pick = <T>(
      obj: Record<string, unknown>,
      keys: string[],
    ): T | undefined => {
      for (const k of keys) {
        const v = obj[k];
        if (v !== undefined && v !== null) return v as T;
      }
      return undefined;
    };
    return {
      summary: {
        status:
          pick<TaxOverview["summary"]["status"]>(summary, ["status"]) ?? "none",
        totalDue: pick<number>(summary, ["totalDue", "total_due"]) ?? null,
        nextDeadline:
          pick<string>(summary, ["nextDeadline", "next_deadline"]) ?? null,
        daysToDeadline:
          pick<number>(summary, [
            "daysToDeadline",
            "days_until_deadline",
            "days_to_deadline",
          ]) ?? null,
        pendingCount:
          pick<number>(summary, ["pendingCount", "pending_count"]) ?? 0,
        overdueCount:
          pick<number>(summary, ["overdueCount", "overdue_count"]) ?? 0,
      },
      obligations: obligations.map((o) => ({
        id: String(o.id ?? o.uuid ?? ""),
        name: pick<string>(o, ["name"]) ?? "",
        type: pick<string>(o, ["type", "tax_type"]) ?? "",
        period:
          pick<string>(o, ["period"]) ??
          `${o.period_start ?? ""} — ${o.period_end ?? ""}`,
        dueDate: pick<string>(o, ["dueDate", "due_date"]) ?? "",
        status: pick<TaxObligation["status"]>(o, ["status"]) ?? "pending",
        amount: pick<number>(o, ["amount", "amount_due"]),
      })),
    };
  }

  // ============================================================================
  // Documents
  // ============================================================================

  private normalizeDocument(raw: Record<string, unknown>): PortalDocument {
    const id = String(raw.id ?? "");
    const size =
      typeof raw.size === "string"
        ? raw.size
        : raw.size_kb != null
          ? `${raw.size_kb} KB`
          : "";
    const downloadUrl =
      typeof raw.downloadUrl === "string"
        ? raw.downloadUrl
        : typeof raw.download_url === "string"
          ? raw.download_url
          : raw.downloadable && id
            ? `/api/portal/documents/${id}/download`
            : "";
    const doc: PortalDocument = {
      id,
      name: String(raw.name ?? raw.file_name ?? ""),
      type: String(raw.type ?? raw.document_type ?? ""),
      category: String(raw.category ?? raw.type ?? raw.document_type ?? ""),
      uploadDate: String(
        raw.uploadDate ?? raw.upload_date ?? raw.created_at ?? "",
      ),
      size,
      status: (raw.status as PortalDocument["status"]) ?? "pending",
      downloadUrl,
    };
    const expiryDate = raw.expiryDate ?? raw.expiry_date;
    if (typeof expiryDate === "string" && expiryDate.length > 0) {
      doc.expiryDate = expiryDate;
    }
    const practiceId = raw.practiceId ?? raw.practice_id;
    if (typeof practiceId === "number") {
      doc.practiceId = practiceId;
    }
    return doc;
  }

  async getDocuments(documentType?: string): Promise<PortalDocument[]> {
    const params = documentType
      ? `?document_type=${encodeURIComponent(documentType)}`
      : "";
    const response = await this.client.request<
      PortalApiResponse<
        Record<string, unknown>[] | { documents?: Record<string, unknown>[] }
      >
    >(`/api/portal/documents${params}`, { method: "GET" });
    const payload = response.data;
    const rawList = Array.isArray(payload)
      ? payload
      : Array.isArray(payload?.documents)
        ? payload.documents
        : [];
    return rawList.map((item) => this.normalizeDocument(item));
  }

  async uploadDocument(
    file: File,
    documentType: string,
    practiceId?: number,
  ): Promise<PortalDocument> {
    return measurePortalAction(
      "document_upload",
      () => ({
        role: this.client.getUserProfile?.()?.role,
        impersonating: this.client.getPortalImpersonation?.() !== null,
      }),
      async () => {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("document_type", documentType);
        if (practiceId) {
          formData.append("practice_id", practiceId.toString());
        }

        const response = await this.client.request<
          PortalApiResponse<Record<string, unknown>>
        >("/api/portal/documents/upload", {
          method: "POST",
          body: formData,
          // Don't set Content-Type - browser will set it with boundary for multipart
        });
        return this.normalizeDocument((response.data ?? {}) as Record<string, unknown>);
      },
    );
  }

  // ============================================================================
  // Messages
  // ============================================================================

  async getMessages(limit = 50, offset = 0): Promise<MessagesResponse> {
    const response = await this.client.request<
      PortalApiResponse<RawMessagesResponse>
    >(`/api/portal/messages?limit=${limit}&offset=${offset}`, {
      method: "GET",
    });
    return normalizeMessagesResponse(response.data!);
  }

  async sendMessage(request: SendMessageRequest): Promise<PortalMessage> {
    return measurePortalAction(
      "message_send",
      () => ({
        role: this.client.getUserProfile?.()?.role,
        impersonating: this.client.getPortalImpersonation?.() !== null,
      }),
      async () => {
        const response = await this.client.request<
          PortalApiResponse<RawPortalMessage>
        >("/api/portal/messages", {
          method: "POST",
          body: JSON.stringify(request),
        });
        return normalizePortalMessage(response.data!);
      },
    );
  }

  async markMessageRead(messageId: number): Promise<void> {
    await this.client.request<PortalApiResponse<void>>(
      `/api/portal/messages/${messageId}/read`,
      {
        method: "POST",
      },
    );
  }

  // ============================================================================
  // Settings
  // ============================================================================

  async getPreferences(): Promise<PortalPreferences> {
    const response = await this.client.request<
      PortalApiResponse<PortalPreferences>
    >("/api/portal/settings", { method: "GET" });
    return response.data!;
  }

  async updatePreferences(
    preferences: Partial<PortalPreferences>,
  ): Promise<PortalPreferences> {
    const response = await this.client.request<
      PortalApiResponse<PortalPreferences>
    >("/api/portal/settings", {
      method: "PATCH",
      body: JSON.stringify(preferences),
    });
    return response.data!;
  }

  // ============================================================================
  // LKPM (Investment Reports)
  // ============================================================================

  async getLKPMHistory(_clientId?: number): Promise<LKPMDraftSummary[]> {
    const response = await this.client.request<
      PortalApiResponse<{ items?: LKPMDraftSummary[] }> & {
        items?: LKPMDraftSummary[];
      }
    >("/api/v1/lkpm/history/me", { method: "GET" });
    return response.items ?? response.data?.items ?? [];
  }

  async getLKPMDraft(
    clientId: number,
    quarter: string,
    year: number,
  ): Promise<LKPMDraft> {
    // Backend returns {success: true, draft: {...}} (no data envelope),
    // unlike most portal endpoints. Cast accordingly.
    const response = await this.client.request<{
      success: boolean;
      draft: LKPMDraft;
    }>(`/api/v1/lkpm/draft/${clientId}/${quarter}?year=${year}`, {
      method: "GET",
    });
    return response.draft;
  }

  async approveLKPMDraft(draftId: number): Promise<{ success: boolean }> {
    const response = await this.client.request<
      PortalApiResponse<{ success?: boolean }> & { status?: string }
    >(`/api/v1/lkpm/approve/${draftId}`, { method: "POST" });
    return {
      success:
        response.success ??
        response.data?.success ??
        (response.status === "approved" || response.status === "ok"
          ? true
          : Boolean(response)),
    };
  }

  // ============================================================================
  // Invitation Flow (Public endpoints)
  // ============================================================================

  async validateInviteToken(token: string): Promise<InviteValidationResponse> {
    // Public endpoint - no auth required (backend allows unauthenticated access)
    const response = await this.client.request<Record<string, unknown>>(
      `/api/portal/invite/validate/${token}`,
      { method: "GET" },
    );
    const raw = ((response.data as Record<string, unknown> | undefined) ??
      response) as Record<string, unknown>;
    const normalized: InviteValidationResponse = {
      valid: Boolean(raw.valid),
    };
    if (raw.error != null) normalized.error = String(raw.error);
    if (raw.message != null) normalized.message = String(raw.message);
    const clientName = raw.clientName ?? raw.client_name;
    if (clientName != null) normalized.clientName = String(clientName);
    if (raw.email != null) normalized.email = String(raw.email);
    const invitationId = raw.invitationId ?? raw.invitation_id;
    if (typeof invitationId === "number") normalized.invitationId = invitationId;
    const clientId = raw.clientId ?? raw.client_id;
    if (typeof clientId === "number") normalized.clientId = clientId;
    return normalized;
  }

  async completeRegistration(
    request: CompleteRegistrationRequest,
  ): Promise<RegistrationResponse> {
    // Public endpoint - no auth required (backend allows unauthenticated access)
    const response = await this.client.request<Record<string, unknown>>(
      "/api/portal/invite/complete",
      {
        method: "POST",
        body: JSON.stringify(request),
      },
    );
    const raw = ((response.data as Record<string, unknown> | undefined) ??
      response) as Record<string, unknown>;
    const normalized: RegistrationResponse = {
      success: Boolean(raw.success ?? true),
      message: String(raw.message ?? ""),
    };
    const userId = raw.userId ?? raw.user_id;
    if (userId != null) normalized.userId = String(userId);
    const redirectTo = raw.redirectTo ?? raw.redirect_to;
    if (redirectTo != null) normalized.redirectTo = String(redirectTo);
    return normalized;
  }

  // ============================================================================
  // Process Timeline
  // ============================================================================

  async getProcessTimeline(practiceId: number): Promise<ProcessTimeline> {
    const response = await this.client.request<
      PortalApiResponse<ProcessTimeline>
    >(`/api/portal/process/${practiceId}/timeline`, { method: "GET" });
    return response.data!;
  }

  /**
   * Portal-scoped list of required documents across the caller's active
   * practices. The workspace uses /api/crm/clients/client/{id}/required-documents
   * which 403s for plain client JWTs — this route resolves client_id from
   * the JWT (or ?as_client= for superusers).
   */
  async getMyRequiredDocuments(): Promise<unknown[]> {
    const response = await this.client.request<PortalApiResponse<unknown[]>>(
      "/api/portal/process/required-documents",
      { method: "GET" },
    );
    return response.data ?? [];
  }

  // ============================================================================
  // Drive Files
  // ============================================================================

  async getDriveFiles(): Promise<DriveFilesResponse> {
    const response = await this.client.request<
      PortalApiResponse<DriveFilesResponse>
    >("/api/portal/drive/files", { method: "GET" });
    return response.data!;
  }

  async getDriveSubfolderFiles(folderId: string): Promise<DriveFilesResponse> {
    const response = await this.client.request<
      PortalApiResponse<DriveFilesResponse>
    >(`/api/portal/drive/files/${folderId}/list`, { method: "GET" });
    return response.data!;
  }

  // ============================================================================
  // Billing
  // ============================================================================

  async getBilling(): Promise<BillingResponse> {
    const response = await this.client.request<
      PortalApiResponse<BillingResponse>
    >("/api/portal/billing", { method: "GET" });
    return response.data!;
  }

  async getInvoicePdfUrl(invoiceId: number): Promise<{ download_url: string }> {
    const response = await this.client.request<
      PortalApiResponse<{ download_url: string }>
    >(`/api/portal/billing/${invoiceId}/pdf-url`, { method: "GET" });
    return response.data!;
  }

  /** Payment proof for one invoice (docs/specs/payment-proof-upload.md §b). */
  async uploadPaymentProof(
    invoiceId: number,
    file: File,
  ): Promise<PaymentProof> {
    const formData = new FormData();
    formData.append("file", file);
    const response = await this.client.request<PortalApiResponse<PaymentProof>>(
      `/api/portal/billing/${invoiceId}/payment-proof`,
      { method: "POST", body: formData },
    );
    return response.data!;
  }

  // ============================================================================
  // Notifications
  // ============================================================================

  async getNotifications(limit = 50): Promise<NotificationsResponse> {
    const response = await this.client.request<
      PortalApiResponse<NotificationsResponse>
    >(`/api/portal/notifications?limit=${limit}`, { method: "GET" });
    return response.data!;
  }

  async markNotificationRead(notificationId: number): Promise<void> {
    await this.client.request<PortalApiResponse<void>>(
      `/api/portal/notifications/${notificationId}/read`,
      { method: "POST" },
    );
  }

  async markAllNotificationsRead(): Promise<void> {
    await this.client.request<PortalApiResponse<void>>(
      "/api/portal/notifications/read-all",
      { method: "POST" },
    );
  }
}

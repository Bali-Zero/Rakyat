import { describe, it, expect, vi, beforeEach } from "vitest";
import { WhatsAppApi } from "./whatsapp.api";
import type { IApiClient } from "../types/api-client.types";

describe("WhatsAppApi", () => {
  let client: IApiClient;
  let whatsappApi: WhatsAppApi;

  beforeEach(() => {
    client = {
      request: vi
        .fn()
        .mockResolvedValue({ success: true, message_id: "wamid.1" }),
    } as unknown as IApiClient;
    whatsappApi = new WhatsAppApi(client);
  });

  it("sends outbound WhatsApp messages using the backend message field", async () => {
    const res = await whatsappApi.sendMessage(
      "+6284000220000",
      "Hello from CRM",
    );

    expect(client.request).toHaveBeenCalledWith("/api/whatsapp/send", {
      method: "POST",
      body: JSON.stringify({
        phone: "+6284000220000",
        message: "Hello from CRM",
      }),
    });
    expect(res).toEqual({ success: true, message_id: "wamid.1" });
  });

  it("includes reply_to when replyToMessageId is provided", async () => {
    await whatsappApi.sendMessage("+6284000220000", "Reply body", "wamid.orig");

    expect(client.request).toHaveBeenCalledWith("/api/whatsapp/send", {
      method: "POST",
      body: JSON.stringify({
        phone: "+6284000220000",
        message: "Reply body",
        reply_to: "wamid.orig",
      }),
    });
  });
});

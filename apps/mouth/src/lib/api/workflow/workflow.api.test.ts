import { describe, it, expect, vi, beforeEach } from "vitest";
import { WorkflowApi } from "./index";
import { ApiClient } from "../api-client";

// Mock fetch globally
const mockFetch = vi.fn();
global.fetch = mockFetch;

describe("WorkflowApi", () => {
  let api: ApiClient;
  let workflowApi: WorkflowApi;
  const baseUrl = "https://api.test.com";

  beforeEach(() => {
    vi.restoreAllMocks();
    mockFetch.mockReset();
    api = new ApiClient(baseUrl);
    workflowApi = new WorkflowApi(api as any);
  });

  describe("enqueue", () => {
    it("should call POST /api/workflow/enqueue with chain_id and payload", async () => {
      const mockResponse = {
        job_id: "550e8400-e29b-41d4-a716-446655440000",
        thread_id: "chain_new_client_onboarding:550e8400",
        chain_id: "chain_new_client_onboarding",
        status: "pending",
      };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => mockResponse,
      });

      const result = await workflowApi.enqueue({
        chain_id: "chain_new_client_onboarding",
        payload: { client_id: 42 },
      });

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/workflow/enqueue"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            chain_id: "chain_new_client_onboarding",
            payload: { client_id: 42 },
          }),
        }),
      );
      expect(result).toEqual(mockResponse);
    });
  });

  describe("getStatus", () => {
    it("should call GET /api/workflow/status/{jobId}", async () => {
      const jobId = "550e8400-e29b-41d4-a716-446655440000";
      const mockResponse = {
        job_id: jobId,
        status: "completed",
        chain_id: "chain_new_client_onboarding",
        thread_id: "chain_new_client_onboarding:1",
        retry_count: 0,
        error_msg: null,
      };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => mockResponse,
      });

      const result = await workflowApi.getStatus(jobId);

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining(`/api/workflow/status/${jobId}`),
        expect.objectContaining({ method: "GET" }),
      );
      expect(result).toEqual(mockResponse);
    });
  });

  describe("listChains", () => {
    it("should call GET /api/workflow/chains", async () => {
      const mockResponse = {
        chains: ["chain_new_client_onboarding", "chain_intel_to_content"],
      };
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: async () => mockResponse,
      });

      const result = await workflowApi.listChains();

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/workflow/chains"),
        expect.objectContaining({ method: "GET" }),
      );
      expect(result).toEqual(mockResponse);
    });
  });
});

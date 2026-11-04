import type { IApiClient } from "../types/api-client.types";

export interface WorkflowEnqueueRequest {
  chain_id: string;
  payload?: Record<string, unknown>;
  thread_id?: string | null;
}

export interface WorkflowEnqueueResponse {
  job_id: string;
  thread_id: string;
  chain_id: string;
  status: string;
}

export interface WorkflowJobStatusResponse {
  job_id: string;
  status: string;
  chain_id: string;
  thread_id: string | null;
  retry_count: number;
  error_msg: string | null;
}

export interface WorkflowChainsResponse {
  chains: string[];
}

export class WorkflowApi {
  constructor(private client: IApiClient) {}

  async enqueue(
    body: WorkflowEnqueueRequest,
  ): Promise<WorkflowEnqueueResponse> {
    return this.client.request<WorkflowEnqueueResponse>(
      "/api/workflow/enqueue",
      {
        method: "POST",
        body: JSON.stringify({
          chain_id: body.chain_id,
          payload: body.payload ?? {},
          ...(body.thread_id !== undefined
            ? { thread_id: body.thread_id }
            : {}),
        }),
      },
    );
  }

  async getStatus(jobId: string): Promise<WorkflowJobStatusResponse> {
    return this.client.request<WorkflowJobStatusResponse>(
      `/api/workflow/status/${encodeURIComponent(jobId)}`,
      {
        method: "GET",
      },
    );
  }

  async listChains(): Promise<WorkflowChainsResponse> {
    return this.client.request<WorkflowChainsResponse>("/api/workflow/chains", {
      method: "GET",
    });
  }
}

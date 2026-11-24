"""Status and scheduler endpoints for autonomous agents."""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.dependencies import get_current_user
from backend.app.utils.crm_utils import is_crm_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autonomous-agents", tags=["autonomous-tier1"])


def _require_agent_admin(user: dict[str, Any]) -> None:
    """Gate scheduler control to admins."""
    if not is_crm_admin(user):
        raise HTTPException(status_code=403, detail="Agent execution requires admin")


agent_executions: dict[str, dict[str, Any]] = {}


class AgentExecutionResponse(BaseModel):
    execution_id: str
    agent_name: str
    status: str
    message: str
    started_at: str
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


@router.get("/status")
async def get_autonomous_agents_status() -> dict[str, Any]:
    """Get status of all Tier 1 autonomous agents with execution statistics."""

    def get_next_run_from_scheduler(agent_id: str) -> str | None:
        """Get next scheduled run time for an agent from the scheduler."""
        try:
            from backend.services.misc.autonomous_scheduler import get_autonomous_scheduler

            scheduler = get_autonomous_scheduler()
            status = scheduler.get_status()
            task_info = status.get("tasks", {}).get(agent_id)

            if task_info and task_info.get("enabled") and task_info.get("last_run"):
                from datetime import datetime, timedelta

                last_run = datetime.fromisoformat(task_info["last_run"])
                interval = task_info.get("interval_seconds", 0)

                if interval > 0:
                    next_run = last_run + timedelta(seconds=interval)
                    return next_run.isoformat()

            return None
        except Exception as e:
            logger.debug("Could not get next_run from scheduler: %s", e)
            return None

    def get_agent_stats(agent_id: str) -> dict[str, Any]:
        """Calculate execution statistics for an agent."""
        agent_execs = [
            (exec_id, data)
            for exec_id, data in agent_executions.items()
            if data.get("agent_name") == agent_id
        ]

        if not agent_execs:
            return {
                "status": "idle",
                "last_run": None,
                "next_run": get_next_run_from_scheduler(agent_id),
                "success_rate": None,
                "total_runs": 0,
                "latest_result": None,
            }

        agent_execs.sort(key=lambda x: x[1].get("started_at", ""), reverse=True)
        _latest_exec_id, latest_exec = agent_execs[0]

        latest_status = latest_exec.get("status", "idle")
        if latest_status == "running":
            status = "running"
        elif latest_status == "failed":
            status = "error"
        else:
            status = "idle"

        total_runs = len(agent_execs)
        successful_runs = sum(1 for _, data in agent_execs if data.get("status") == "completed")
        success_rate = (successful_runs / total_runs * 100) if total_runs > 0 else None
        last_run = latest_exec.get("completed_at") or latest_exec.get("started_at")
        latest_result = latest_exec.get("result")

        return {
            "status": status,
            "last_run": last_run,
            "next_run": get_next_run_from_scheduler(agent_id),
            "success_rate": round(success_rate, 1) if success_rate is not None else None,
            "total_runs": total_runs,
            "latest_result": latest_result,
        }

    agents_data = [
        {
            "name": "Conversation Quality Trainer",
            "description": "Learns from successful conversations and improves prompts",
            **get_agent_stats("conversation_trainer"),
        },
        {
            "name": "Client LTV Predictor & Nurturer",
            "description": "Predicts client value and sends personalized nurturing messages",
            **get_agent_stats("client_value_predictor"),
        },
        {
            "name": "Knowledge Graph Builder",
            "description": "Extracts entities and relationships from all data sources",
            **get_agent_stats("knowledge_graph_builder"),
        },
    ]

    return {
        "success": True,
        "tier": 1,
        "total_agents": 3,
        "agents": agents_data,
        "recent_executions": len(agent_executions),
        "timestamp": datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(),
    }


@router.get("/executions/{execution_id}", response_model=AgentExecutionResponse)
async def get_execution_status(execution_id: str) -> AgentExecutionResponse:
    """Get status of a specific agent execution."""
    if execution_id not in agent_executions:
        raise HTTPException(status_code=404, detail="Execution not found")

    execution_data = agent_executions[execution_id].copy()
    execution_data["execution_id"] = execution_id
    return AgentExecutionResponse(**execution_data)


@router.get("/executions")
async def list_executions(limit: int = 20) -> dict[str, Any]:
    """List recent agent executions."""
    executions = sorted(
        agent_executions.items(),
        key=lambda x: x[1].get("started_at", ""),
        reverse=True,
    )[:limit]

    return {
        "success": True,
        "executions": [{**data, "execution_id": exec_id} for exec_id, data in executions],
        "total": len(agent_executions),
    }


@router.get("/scheduler/status")
async def get_scheduler_status() -> dict[str, Any]:
    """Get status of the autonomous scheduler and its registered tasks."""
    try:
        from backend.services.misc.autonomous_scheduler import get_autonomous_scheduler

        scheduler = get_autonomous_scheduler()
        status = scheduler.get_status()

        return {
            "success": True,
            "scheduler": status,
            "timestamp": datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(),
        }
    except Exception as e:
        logger.error("Failed to get scheduler status: %s", e)
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now(tz=timezone.utc).replace(tzinfo=None).isoformat(),
        }


@router.post("/scheduler/task/{task_name}/enable")
async def enable_scheduler_task(
    task_name: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Enable a scheduled task (admin-only)."""
    _require_agent_admin(current_user)

    try:
        from backend.services.misc.autonomous_scheduler import get_autonomous_scheduler

        scheduler = get_autonomous_scheduler()
        success = scheduler.enable_task(task_name)

        if success:
            return {
                "success": True,
                "message": f"Task '{task_name}' enabled",
                "task_name": task_name,
            }
        raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to enable task: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/scheduler/task/{task_name}/disable")
async def disable_scheduler_task(
    task_name: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Disable a scheduled task (admin-only)."""
    _require_agent_admin(current_user)

    try:
        from backend.services.misc.autonomous_scheduler import get_autonomous_scheduler

        scheduler = get_autonomous_scheduler()
        success = scheduler.disable_task(task_name)

        if success:
            return {
                "success": True,
                "message": f"Task '{task_name}' disabled",
                "task_name": task_name,
            }
        raise HTTPException(status_code=404, detail=f"Task '{task_name}' not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to disable task: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

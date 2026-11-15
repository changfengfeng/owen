import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from cloud_agent.auth import Principal, principal
from cloud_agent.models import Task
from cloud_agent.queue import TERMINAL, TaskQueue

router = APIRouter(prefix="/api", tags=["tasks"])


def queue(request: Request) -> TaskQueue:
    return request.app.state.queue


def task_json(task: Task, detail: bool = False) -> dict:
    result = {
        key: getattr(task, key)
        for key in (
            "id",
            "prompt",
            "repo_url",
            "status",
            "budget_tokens",
            "used_tokens",
            "cancel_requested",
            "created_at",
            "started_at",
            "finished_at",
        )
    }
    if detail:
        result.update(summary=task.summary, error=task.error, patch=task.patch)
    return result


class TaskInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=12_000)
    repo_url: str = Field(default="", max_length=300)


@router.get("/usage")
def usage(actor: Principal = Depends(principal), tasks: TaskQueue = Depends(queue)):
    return tasks.usage(actor.tenant_id)


@router.get("/tasks")
def list_tasks(
    before: int | None = None,
    actor: Principal = Depends(principal),
    tasks: TaskQueue = Depends(queue),
):
    return {"tasks": [task_json(task) for task in tasks.list(actor.tenant_id, before)]}


@router.post("/tasks", status_code=201)
def create_task(
    payload: TaskInput,
    idempotency_key: str | None = Header(default=None),
    actor: Principal = Depends(principal),
    tasks: TaskQueue = Depends(queue),
):
    return task_json(
        tasks.submit(
            actor.tenant_id, actor.user_id, payload.prompt, payload.repo_url, idempotency_key
        )
    )


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str, actor: Principal = Depends(principal), tasks: TaskQueue = Depends(queue)
):
    return task_json(tasks.get(actor.tenant_id, task_id), detail=True)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str, actor: Principal = Depends(principal), tasks: TaskQueue = Depends(queue)
):
    return task_json(tasks.cancel(actor.tenant_id, task_id))


@router.get("/tasks/{task_id}/patch")
def download_patch(
    task_id: str, actor: Principal = Depends(principal), tasks: TaskQueue = Depends(queue)
):
    task = tasks.get(actor.tenant_id, task_id)
    if task.status not in TERMINAL:
        raise HTTPException(409, "Task is still executing")
    return Response(
        task.patch,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{task.id}.patch"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/tasks/{task_id}/events")
async def events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    actor: Principal = Depends(principal),
    tasks: TaskQueue = Depends(queue),
):
    tasks.get(actor.tenant_id, task_id)
    if last_event_id:
        try:
            after = max(after, int(last_event_id))
        except ValueError:
            raise HTTPException(422, "Invalid event cursor") from None

    async def stream():
        cursor, ticks = after, 0
        while not await request.is_disconnected():
            # Recheck expiry/revocation during long-lived connections.
            if ticks % 15 == 0:
                try:
                    principal(request)
                except HTTPException:
                    return
            rows = tasks.events(actor.tenant_id, task_id, cursor)
            for event in rows:
                data = {
                    "kind": event.kind,
                    "data": json.loads(event.data),
                    "created_at": event.created_at,
                }
                yield f"id: {event.id}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                cursor = event.id
            if len(rows) < 200 and tasks.get(actor.tenant_id, task_id).status in TERMINAL:
                yield "event: complete\ndata: {}\n\n"
                return
            if not rows:
                yield ": heartbeat\n\n"
            ticks += 1
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )

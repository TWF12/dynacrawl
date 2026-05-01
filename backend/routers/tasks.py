from fastapi import APIRouter, HTTPException
from backend.schemas import TaskCreateRequest, TaskResponse, TaskResultResponse, UrlRecordResponse, UpInfoResponse, VideoInfoResponse, CommentResponse
from backend.services import task_service
from backend.crawler.dispatcher import get_dispatcher

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(req: TaskCreateRequest):
    dispatcher = get_dispatcher()
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="调度器未就绪")
    task = await task_service.create_task(req.scene.value, req.input_value, dispatcher)
    return task


@router.get("", response_model=dict)
async def list_tasks(page: int = 1, page_size: int = 20):
    tasks, total = await task_service.get_tasks(page, page_size)
    return {
        "items": [TaskResponse.model_validate(t) for t in tasks],
        "total": total, "page": page, "page_size": page_size,
    }


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    task = await task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    deleted = await task_service.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"message": "任务已删除"}


@router.get("/{task_id}/results", response_model=TaskResultResponse)
async def get_task_results(task_id: str):
    results = await task_service.get_task_results(task_id)
    if not results.get("task"):
        raise HTTPException(status_code=404, detail="任务不存在")
    return TaskResultResponse(
        task=TaskResponse.model_validate(results["task"]),
        url_records=[UrlRecordResponse.model_validate(r) for r in results["url_records"]],
        up_infos=[UpInfoResponse.model_validate(u) for u in results["up_infos"]] if results["up_infos"] else None,
        video_infos=[VideoInfoResponse.model_validate(v) for v in results["video_infos"]] if results["video_infos"] else None,
        comments=[CommentResponse.model_validate(c) for c in results["comments"]] if results["comments"] else None,
    )

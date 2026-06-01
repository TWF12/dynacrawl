from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from backend.services import export_service

router = APIRouter(prefix="/api/tasks", tags=["export"])


@router.get("/{task_id}/export/csv")
async def export_csv(task_id: str):
    csv_content = await export_service.export_csv(task_id)
    if csv_content is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=task_{task_id}.csv"},
    )


@router.get("/{task_id}/export/json")
async def export_json(task_id: str):
    json_content = await export_service.export_json(task_id)
    if json_content is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return PlainTextResponse(
        content=json_content,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=task_{task_id}.json"},
    )

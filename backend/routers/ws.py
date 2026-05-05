import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from backend.schemas import TaskProgressMessage

logger = logging.getLogger(__name__)
router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, task_id: str, websocket: WebSocket):
        await websocket.accept()
        if task_id not in self._connections:
            self._connections[task_id] = []
        self._connections[task_id].append(websocket)

    def disconnect(self, task_id: str, websocket: WebSocket):
        if task_id in self._connections:
            self._connections[task_id].remove(websocket)
            if not self._connections[task_id]:
                del self._connections[task_id]

    async def broadcast(self, task_id: str, message: dict):
        if task_id not in self._connections:
            return
        dead = []
        for ws in self._connections[task_id]:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(task_id, ws)


manager = ConnectionManager()

# 缓存每个任务最新的进度, API 查询时可立即同步
_progress_cache: dict[str, str] = {}
_video_cache: dict[str, tuple[int, int]] = {}


def get_cached_progress(task_id: str) -> str:
    return _progress_cache.get(task_id, "")


def get_cached_video_progress(task_id: str) -> tuple[int, int]:
    return _video_cache.get(task_id, (0, 0))


async def progress_callback(task_id: str, completed: int, total: int, failed: int, message: str,
                              video_current: int = 0, video_total: int = 0):
    _progress_cache[task_id] = message
    if video_total > 0:
        _video_cache[task_id] = (video_current, video_total)
    msg = TaskProgressMessage(
        type="progress", task_id=task_id, completed_urls=completed,
        total_urls=total, failed_urls=failed, message=message,
        video_current=video_current, video_total=video_total,
    )
    await manager.broadcast(task_id, msg.model_dump())


@router.websocket("/ws/tasks/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    await manager.connect(task_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(task_id, websocket)
    except Exception as e:
        logger.error(f"WebSocket 异常 task_id={task_id}: {e}")
        manager.disconnect(task_id, websocket)

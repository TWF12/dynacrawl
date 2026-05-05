from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class SceneType(str, Enum):
    UP_INFO = "up_info"
    VIDEO_DETAIL = "video_detail"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskCreateRequest(BaseModel):
    scene: SceneType
    input_value: str = Field(..., min_length=1, max_length=255)


class TaskResponse(BaseModel):
    id: str
    scene: str
    input_value: str
    total_urls: int
    completed_urls: int
    failed_urls: int
    status: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class UrlRecordResponse(BaseModel):
    id: int
    task_id: str
    url: str
    url_type: str
    status: str
    error_msg: Optional[str] = None
    retry_count: int
    model_config = {"from_attributes": True}


class UpInfoResponse(BaseModel):
    id: int
    task_id: str
    uid: str
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None
    follower_count: Optional[int] = None
    video_count: Optional[int] = None
    collected_at: datetime
    model_config = {"from_attributes": True}


class VideoInfoResponse(BaseModel):
    id: int
    task_id: str
    bv_id: str
    title: Optional[str] = None
    play_count: Optional[int] = None
    like_count: Optional[int] = None
    coin_count: Optional[int] = None
    danmaku_count: Optional[int] = None
    comment_count: Optional[int] = None
    collected_at: datetime
    model_config = {"from_attributes": True}


class CommentResponse(BaseModel):
    id: int
    task_id: str
    bv_id: str
    username: Optional[str] = None
    content: Optional[str] = None
    like_count: Optional[int] = None
    posted_at: Optional[str] = None
    collected_at: datetime
    model_config = {"from_attributes": True}


class TaskResultResponse(BaseModel):
    task: TaskResponse
    url_records: list[UrlRecordResponse]
    up_infos: Optional[list[UpInfoResponse]] = None
    video_infos: Optional[list[VideoInfoResponse]] = None
    comments: Optional[list[CommentResponse]] = None
    progress_message: str = ""


class TaskProgressMessage(BaseModel):
    type: str
    task_id: str
    completed_urls: int = 0
    total_urls: int = 0
    failed_urls: int = 0
    message: str = ""
    video_current: int = 0
    video_total: int = 0

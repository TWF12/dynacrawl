import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship

from backend.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


def gen_uuid() -> str:
    return str(uuid.uuid4())


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    scene = Column(String(20), nullable=False)
    input_value = Column(String(255), nullable=False)
    total_urls = Column(Integer, default=0)
    completed_urls = Column(Integer, default=0)
    failed_urls = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    url_records = relationship("UrlRecord", back_populates="task", cascade="all, delete-orphan")
    up_infos = relationship("UpInfo", back_populates="task", cascade="all, delete-orphan")
    video_infos = relationship("VideoInfo", back_populates="task", cascade="all, delete-orphan")
    comments = relationship("Comment", back_populates="task", cascade="all, delete-orphan")


class UrlRecord(Base):
    __tablename__ = "url_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    url = Column(String(1024), nullable=False)
    url_type = Column(String(20), nullable=False)
    status = Column(String(20), default="pending")
    error_msg = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    task = relationship("Task", back_populates="url_records")


class UpInfo(Base):
    __tablename__ = "up_infos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    uid = Column(String(50), nullable=False)
    nickname = Column(String(255), nullable=True)
    avatar_url = Column(String(1024), nullable=True)
    follower_count = Column(Integer, nullable=True)
    video_count = Column(Integer, nullable=True)
    raw_data = Column(JSON, nullable=True)
    collected_at = Column(DateTime, default=datetime.now)

    task = relationship("Task", back_populates="up_infos")


class VideoInfo(Base):
    __tablename__ = "video_infos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    bv_id = Column(String(50), nullable=False)
    title = Column(String(512), nullable=True)
    play_count = Column(Integer, nullable=True)
    like_count = Column(Integer, nullable=True)
    coin_count = Column(Integer, nullable=True)
    danmaku_count = Column(Integer, nullable=True)
    comment_count = Column(Integer, nullable=True)
    raw_data = Column(JSON, nullable=True)
    collected_at = Column(DateTime, default=datetime.now)

    task = relationship("Task", back_populates="video_infos")


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    bv_id = Column(String(50), nullable=False)
    username = Column(String(255), nullable=True)
    content = Column(Text, nullable=True)
    like_count = Column(Integer, nullable=True)
    posted_at = Column(String(50), nullable=True)
    collected_at = Column(DateTime, default=datetime.now)

    task = relationship("Task", back_populates="comments")

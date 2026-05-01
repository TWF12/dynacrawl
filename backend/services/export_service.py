import csv, json, io
from typing import Optional
from backend.services import task_service


async def export_csv(task_id: str) -> Optional[str]:
    results = await task_service.get_task_results(task_id)
    if not results.get("task"): return None

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)

    task = results["task"]
    if task.scene == "up_info":
        writer.writerow(["数据类型", "UID", "昵称", "头像URL", "粉丝数", "视频数", "采集时间"])
        for up in results.get("up_infos", []):
            writer.writerow(["UP主信息", up.uid, up.nickname or "", up.avatar_url or "",
                              up.follower_count or 0, up.video_count or 0,
                              up.collected_at.isoformat() if up.collected_at else ""])
        writer.writerow([])
        writer.writerow(["数据类型", "BV号", "标题", "播放量", "采集时间"])
        for v in results.get("video_infos", []):
            writer.writerow(["视频", v.bv_id or "", v.title or "", v.play_count or 0,
                              v.collected_at.isoformat() if v.collected_at else ""])
    elif task.scene == "video_detail":
        writer.writerow(["数据类型", "BV号", "标题", "播放量", "点赞数", "投币数", "弹幕数", "评论数", "采集时间"])
        for v in results.get("video_infos", []):
            writer.writerow(["视频信息", v.bv_id or "", v.title or "", v.play_count or 0,
                              v.like_count or 0, v.coin_count or 0, v.danmaku_count or 0,
                              v.comment_count or 0, v.collected_at.isoformat() if v.collected_at else ""])
        writer.writerow([])
        writer.writerow(["数据类型", "BV号", "用户名", "评论内容", "点赞数", "发布时间", "采集时间"])
        for c in results.get("comments", []):
            writer.writerow(["评论", c.bv_id or "", c.username or "", c.content or "",
                              c.like_count or 0, c.posted_at or "",
                              c.collected_at.isoformat() if c.collected_at else ""])
    return output.getvalue()


async def export_json(task_id: str) -> Optional[str]:
    results = await task_service.get_task_results(task_id)
    if not results.get("task"): return None

    task = results["task"]
    output = {"task": {"id": task.id, "scene": task.scene, "input_value": task.input_value,
                        "status": task.status, "total_urls": task.total_urls,
                        "completed_urls": task.completed_urls, "failed_urls": task.failed_urls,
                        "created_at": task.created_at.isoformat()}}

    if task.scene == "up_info":
        output["up_infos"] = [{"uid": u.uid, "nickname": u.nickname, "avatar_url": u.avatar_url,
                                "follower_count": u.follower_count, "video_count": u.video_count,
                                "collected_at": u.collected_at.isoformat() if u.collected_at else ""}
                               for u in results.get("up_infos", [])]
        output["video_infos"] = [{"bv_id": v.bv_id, "title": v.title, "play_count": v.play_count,
                                   "collected_at": v.collected_at.isoformat() if v.collected_at else ""}
                                  for v in results.get("video_infos", [])]
    elif task.scene == "video_detail":
        output["video_infos"] = [{"bv_id": v.bv_id, "title": v.title, "play_count": v.play_count,
                                   "like_count": v.like_count, "coin_count": v.coin_count,
                                   "danmaku_count": v.danmaku_count, "comment_count": v.comment_count,
                                   "collected_at": v.collected_at.isoformat() if v.collected_at else ""}
                                  for v in results.get("video_infos", [])]
        output["comments"] = [{"bv_id": c.bv_id, "username": c.username, "content": c.content,
                                "like_count": c.like_count, "posted_at": c.posted_at,
                                "collected_at": c.collected_at.isoformat() if c.collected_at else ""}
                               for c in results.get("comments", [])]

    return json.dumps(output, ensure_ascii=False, indent=2)

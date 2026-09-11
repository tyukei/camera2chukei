"""Finger Camera — UnitV2 側の中継サーバ。

実装順 3 までの範囲。GET / と GET /stream と POST /shoot と GET /status。
/config と /hud は実装順 5 と 7 で足す（api.md 参照）。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import queue
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from server.camera import (BOUNDARY, SHOT_FPS, SHOT_SIZE, STREAM_FPS, STREAM_SIZE,
                           Camera, ShotCamera, ffmpeg_command, multipart,
                           require_ffmpeg)

STATIC = Path(__file__).parent / "static"
OUT = Path("out")
MAX_ATTEMPTS = 3  # 撮影に失敗したらキューに戻して、この回数まで粘る

log = logging.getLogger("fingercam")


@dataclass
class Job:
    """1 回の撮影。state は api.md の queued|uploading|ok|error。"""

    rect: list[float]
    id: str = field(default_factory=lambda: (
        f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)[:3]}"))
    state: str = "queued"
    attempts: int = 0


jobs: queue.Queue[Job] = queue.Queue()
last_job: Job | None = None
last_lock = threading.Lock()


class ShootBody(BaseModel):
    rect: list[float]


def validate(rect: list[float]) -> list[float]:
    """rect は [x, y, w, h] の 0.0〜1.0。ピクセルが来たらここで弾く。"""
    if len(rect) != 4:
        raise HTTPException(400, f"rect は [x, y, w, h] の 4 要素。来たのは {len(rect)} 個")
    if not all(0.0 <= v <= 1.0 for v in rect) or rect[2] <= 0 or rect[3] <= 0:
        raise HTTPException(400, f"rect が正規化値として不正: {rect}")
    return rect


def save_crop(job: Job, frame: bytes) -> Path:
    """rect で切り出して out/<id>.jpg に保存する。"""
    img = Image.open(io.BytesIO(frame))
    x, y, w, h = job.rect
    left = min(round(x * img.width), img.width - 1)
    top = min(round(y * img.height), img.height - 1)
    right = max(min(round((x + w) * img.width), img.width), left + 1)
    bottom = max(min(round((y + h) * img.height), img.height), top + 1)
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{job.id}.jpg"
    img.crop((left, top, right, bottom)).save(path, quality=92)
    return path


def finish(job: Job, state: str) -> None:
    global last_job
    job.state = state
    with last_lock:
        last_job = job


def worker(camera: ShotCamera) -> None:
    """キューを 1 件ずつ処理する。失敗は握り潰さず、キューに戻してログに出す。"""
    while (job := jobs.get()) is not None:
        try:
            path = save_crop(job, camera.grab())
            finish(job, "ok")
            log.info("保存 %s rect=%s", path, [round(v, 3) for v in job.rect])
        except Exception:
            job.attempts += 1
            log.exception("撮影に失敗 %s (%d 回目)", job.id, job.attempts)
            if job.attempts < MAX_ATTEMPTS:
                time.sleep(0.5)
                jobs.put(job)
            else:
                finish(job, "error")
                log.error("撮影を諦めた %s", job.id)
        finally:
            jobs.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    camera: ShotCamera = app.state.open_shot_camera()
    camera.start()
    threading.Thread(target=worker, args=(camera,), daemon=True, name="shoot").start()
    yield
    camera.close()


app = FastAPI(title="Finger Camera", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    """アプリ本体。"""
    return FileResponse(STATIC / "index.html")


@app.get("/stream")
async def stream() -> StreamingResponse:
    """MJPEG。--mock では PC の内蔵カメラ。"""
    open_camera: Callable[[], Camera] = app.state.open_camera
    try:
        camera = open_camera()
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    first = await asyncio.to_thread(camera.read)
    if first is None:  # 起動失敗はヘッダを返す前に 503 にする
        detail = camera.failure()
        camera.close()
        log.error("%s", detail)
        raise HTTPException(503, detail)
    return StreamingResponse(
        multipart(camera, first),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "Age": "0"},
    )


@app.post("/shoot", status_code=202)
def shoot(body: ShootBody) -> dict[str, str]:
    """撮影を積むだけ。切り出しと保存は非同期。"""
    job = Job(rect=validate(body.rect))
    jobs.put(job)
    log.info("撮影を受けた %s rect=%s", job.id, [round(v, 3) for v in job.rect])
    return {"id": job.id, "status": job.state}


@app.get("/status")
def status() -> dict[str, object]:
    """キューの深さと直近の 1 件。text は Gemini 用で実装順 8。"""
    with last_lock:
        job = last_job
    last = None if job is None else {"id": job.id, "state": job.state, "text": None}
    return {"queue": jobs.qsize(), "last": last}


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m server.app")
    p.add_argument("--mock", action="store_true", help="PC の内蔵カメラを使う")
    p.add_argument("--timercam", metavar="URL", help="TimerCam の URL（実機・実装順 4）")
    p.add_argument("--device", default="0", help="カメラ。macOS は番号、Linux は /dev/video*")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.timercam:
        p.error("--timercam は実装順 4 で対応する。いまは --mock だけ動く")
    if not args.mock:
        p.error("--mock を付ける。実機の中継はまだ無い")
    require_ffmpeg()

    app.state.open_camera = lambda: Camera(
        ffmpeg_command(args.device, STREAM_SIZE, STREAM_FPS))
    app.state.open_shot_camera = lambda: ShotCamera(
        ffmpeg_command(args.device, SHOT_SIZE, SHOT_FPS))
    log.info("mock モード: /stream も /shoot も PC の内蔵カメラ (device=%s)", args.device)
    log.info("http://localhost:%d を開く", args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

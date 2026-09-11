"""カメラ。ffmpeg を回して JPEG フレームを取り出す。

--mock では PC の内蔵カメラ。実装順 4 で /stream は TimerCam の中継に差し替わるが、
撮影用の ShotCamera は UnitV2 自前のカメラとしてこのまま残る。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator

log = logging.getLogger("fingercam")

BOUNDARY = "frame"

STREAM_SIZE = (640, 480)  # TimerCam の VGA に合わせる
STREAM_FPS = 10
SHOT_SIZE = (640, 480)    # mock では /stream と同じ画角。1080p 化は実装順 4-5
SHOT_FPS = 5


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg が無い。--mock には ffmpeg が要る "
                           "(macOS なら brew install ffmpeg)")


def ffmpeg_command(device: str, size: tuple[int, int], fps: int) -> list[str]:
    """内蔵カメラを mpjpeg にして標準出力へ流す ffmpeg のコマンド。"""
    w, h = size
    if sys.platform == "darwin":
        source = ["-f", "avfoundation", "-framerate", "30",
                  "-video_size", f"{w}x{h}", "-i", f"{device}:none"]
    else:
        source = ["-f", "v4l2", "-video_size", f"{w}x{h}", "-i", device]
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", *source,
            "-r", str(fps), "-q:v", "5", "-f", "mpjpeg", "pipe:1"]


def read_mpjpeg(stdout) -> Iterator[bytes]:
    """ffmpeg の mpjpeg 出力を 1 フレームずつ JPEG バイト列に切り出す。"""
    while True:
        line = stdout.readline()
        if not line:
            return
        if not line.lower().startswith(b"content-length:"):
            continue
        size = int(line.split(b":", 1)[1])
        while stdout.readline().strip():
            pass  # ヘッダ終端の空行まで読み飛ばす
        frame = stdout.read(size)
        if len(frame) < size:
            log.warning("フレームが途中で切れた: %d/%d バイト", len(frame), size)
            return
        yield frame


class Camera:
    """ffmpeg を 1 本抱える。close() を呼ばないとカメラを掴んだままになる。"""

    def __init__(self, cmd: list[str], label: str = "配信用") -> None:
        self.label = label
        log.info("%sカメラ起動: %s", label, " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.frames = read_mpjpeg(self.proc.stdout)
        self.count = 0
        self.closed = False

    def read(self) -> bytes | None:
        """次のフレーム。ffmpeg が止まっていれば None。呼び出しはブロックする。"""
        frame = next(self.frames, None)
        if frame is not None:
            self.count += 1
        return frame

    def failure(self) -> str:
        """フレームが途切れた理由。ffmpeg の stderr から拾う。"""
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
        if self.count == 0:
            return f"{self.label}カメラからフレームが来ない: {err or 'ffmpeg が即終了した'}"
        return f"{self.label}カメラが {self.count} フレームで止まった: {err or '理由不明'}"

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.proc.kill()
        self.proc.wait()
        log.info("%sカメラ停止 (%d フレーム)", self.label, self.count)


class ShotCamera:
    """撮影用に開きっぱなしにするカメラ。最新の 1 枚だけ持つ。

    /shoot のたびに ffmpeg を起動すると立ち上がりで 1〜2 秒ずれて、狙った瞬間が
    撮れない。常時回しておいて最新フレームを返す。
    """

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.latest: bytes | None = None
        self.error: str | None = None
        self.camera: Camera | None = None
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="shotcam")

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            self.camera = Camera(self.cmd, "撮影用")
            while (frame := self.camera.read()) is not None:
                with self.lock:
                    self.latest = frame
                self.ready.set()
            reason = self.camera.failure()
        except Exception as e:  # スレッドの中で消えないよう必ずログに出す
            log.exception("撮影用カメラのスレッドが落ちた")
            reason = f"撮影用カメラのスレッドが落ちた: {e!r}"
        log.error("%s", reason)
        with self.lock:
            self.error = reason
        self.ready.set()  # 待っている撮影を待たせ続けない

    def grab(self, timeout: float = 5.0) -> bytes:
        """最新フレーム。取れないときは理由を付けて例外にする。"""
        if not self.ready.wait(timeout):
            raise RuntimeError("撮影用カメラの立ち上がりがタイムアウトした")
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            if self.latest is None:
                raise RuntimeError("撮影用カメラがまだ 1 枚も出していない")
            return self.latest

    def close(self) -> None:
        if self.camera:
            self.camera.close()


async def multipart(camera: Camera, first: bytes) -> AsyncIterator[bytes]:
    """JPEG の列を multipart/x-mixed-replace のボディにする。read_mpjpeg の逆。

    切断されると呼び出し側がこのジェネレータを閉じるので、finally で必ず止める。
    """
    try:
        frame: bytes | None = first
        while frame is not None:
            yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                   f"Content-Length: {len(frame)}\r\n\r\n").encode()
            yield frame
            yield b"\r\n"
            frame = await asyncio.to_thread(camera.read)
        log.error("%s", camera.failure())
    finally:
        camera.close()


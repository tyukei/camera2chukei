// 実装順 3 まで: /stream の MJPEG に MediaPipe HandLandmarker をかけ、
// 両手で作った枠を canvas に描き、1 秒静止したら /shoot を叩く。
import { FilesetResolver, HandLandmarker }
  from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.0.1/vision_bundle.mjs";

const WASM = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.0.1/wasm";
const MODEL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker" +
              "/hand_landmarker/float16/1/hand_landmarker.task";

const THUMB_TIP = 4;
const INDEX_TIP = 8;
const DETECT_FPS = 15;
const HAND_COLORS = ["#4dd0e1", "#ff80ab"];
const DWELL_MS = 1000;      // これだけ静止したらシャッター
const MOVE_TOL = 0.08;      // 枠の対角長に対するズレが この比 以内なら静止とみなす
const COOLDOWN_MS = 3000;   // 撮った後、次を撮るまで待つ

const stream = document.getElementById("stream");
const overlay = document.getElementById("overlay");
const ctx = overlay.getContext("2d");
const statusEl = document.getElementById("status");

// MediaPipe に渡すフレームの写し。<img> の MJPEG をここに毎回コピーする。
const frame = document.createElement("canvas");
const frameCtx = frame.getContext("2d");

let anchor = null, anchorAt = 0;  // 静止判定の基準にしている枠と、その開始時刻
let cooldownUntil = 0, shooting = false, lastShot = "";

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle("error", isError);
}

async function createLandmarker() {
  const fileset = await FilesetResolver.forVisionTasks(WASM);
  const make = (delegate) => HandLandmarker.createFromOptions(fileset, {
    baseOptions: { modelAssetPath: MODEL, delegate },
    runningMode: "VIDEO",
    numHands: 2,
  });
  try {
    return await make("GPU");
  } catch (e) {
    console.warn("GPU デリゲートが使えない。CPU に落とす", e);
    return make("CPU");
  }
}

/** MJPEG の現フレームを写して返す。まだ届いていなければ null。 */
function grabFrame() {
  const { naturalWidth: w, naturalHeight: h } = stream;
  if (!w || !h) return null;
  if (frame.width !== w || frame.height !== h) {
    frame.width = overlay.width = w;
    frame.height = overlay.height = h;
  }
  frameCtx.drawImage(stream, 0, 0);
  return frame;
}

/** 両手の親指先と人差し指先を囲む矩形。正規化 [x, y, w, h]、TimerCam 座標。 */
function handsToRect(hands) {
  if (hands.length < 2) return null;
  const pts = hands.slice(0, 2).flatMap((lm) => [lm[THUMB_TIP], lm[INDEX_TIP]]);
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return [x, y, Math.max(...xs) - x, Math.max(...ys) - y];
}

/** 正規化座標を canvas の画素へ。 */
const px = (v) => v * overlay.width;
const py = (v) => v * overlay.height;

function drawHand(lm, color) {
  ctx.fillStyle = ctx.strokeStyle = color;
  for (const p of lm) {
    ctx.beginPath();
    ctx.arc(px(p.x), py(p.y), 3, 0, Math.PI * 2);
    ctx.fill();
  }
  // 親指先と人差し指先を結び、枠の角として何を見ているかを示す。
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(px(lm[THUMB_TIP].x), py(lm[THUMB_TIP].y));
  ctx.lineTo(px(lm[INDEX_TIP].x), py(lm[INDEX_TIP].y));
  ctx.stroke();
}

function drawRect([x, y, w, h], progress) {
  const locked = progress >= 1;
  ctx.strokeStyle = locked ? "#7CFF6B" : "#FFD24D";
  ctx.lineWidth = locked ? 5 : 3;
  ctx.strokeRect(px(x), py(y), px(w), py(h));
  ctx.fillStyle = locked ? "rgba(124, 255, 107, 0.12)" : "rgba(255, 210, 77, 0.07)";
  ctx.fillRect(px(x), py(y), px(w), py(h));
  if (progress > 0 && !locked) {
    ctx.strokeStyle = "#7CFF6B"; // 上辺をドウェルの進捗バーとして使う
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.moveTo(px(x), py(y));
    ctx.lineTo(px(x + w * progress), py(y));
    ctx.stroke();
  }
}

/** 2 つの枠のズレ。枠の対角長に対する比で返す。 */
function drift(a, b) {
  return Math.max(...a.map((v, i) => Math.abs(v - b[i]))) /
         (Math.hypot(a[2], a[3]) || 1);
}

/** 静止していれば 0→1 の進み具合、動いていれば 0。 */
function dwellProgress(rect, now) {
  if (!rect) return (anchor = null), 0;
  if (!anchor || drift(anchor, rect) > MOVE_TOL) {
    [anchor, anchorAt] = [rect, now];
    return 0;
  }
  return Math.min(1, (now - anchorAt) / DWELL_MS);
}

async function shoot(rect) {
  shooting = true;
  try {
    const res = await fetch("/shoot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rect: rect.map((v) => Number(v.toFixed(4))) }),
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    lastShot = `撮った ${(await res.json()).id}`;
  } catch (e) {
    console.error(e);
    lastShot = `/shoot 失敗: ${e.message}`;
  } finally {
    shooting = false;
  }
}

const fmt = (rect) => "[" + rect.map((v) => v.toFixed(2)).join(", ") + "]";

function statusText(hands, rect, progress, now) {
  if (now < cooldownUntil && lastShot) return lastShot;
  if (!rect) return hands.length === 1 ? "片手だけ検出。対角をもう片方の手で" : "手を探している";
  if (shooting) return `枠 ${fmt(rect)} 撮影中…`;
  if (progress >= 1) return `枠 ${fmt(rect)} ロック`;
  const rest = ((DWELL_MS * (1 - progress)) / 1000).toFixed(1);
  return progress > 0 ? `枠 ${fmt(rect)} あと ${rest}s` : `枠 ${fmt(rect)} 動いている`;
}

function draw(hands, now) {
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  hands.forEach((lm, i) => drawHand(lm, HAND_COLORS[i % HAND_COLORS.length]));
  const rect = handsToRect(hands);
  const progress = dwellProgress(rect, now);
  if (rect) drawRect(rect, progress);
  if (progress >= 1 && !shooting && now >= cooldownUntil) {
    cooldownUntil = now + COOLDOWN_MS;
    anchor = null; // 撮ったら基準をリセットして、離してからまた狙わせる
    shoot(rect);
  }
  setStatus(statusText(hands, rect, progress, now));
}

async function main() {
  stream.addEventListener("error", () =>
    setStatus("/stream に繋がらない。サーバのログを見る", true));

  setStatus("MediaPipe を読み込み中…");
  let landmarker;
  try {
    landmarker = await createLandmarker();
  } catch (e) {
    console.error(e);
    setStatus(`HandLandmarker を作れない: ${e.message}`, true);
    return;
  }
  setStatus("手を探している");

  let lastDetect = 0;
  const loop = (now) => {
    requestAnimationFrame(loop);
    if (now - lastDetect < 1000 / DETECT_FPS) return;
    const src = grabFrame();
    if (!src) return;
    lastDetect = now;
    try {
      draw(landmarker.detectForVideo(src, now).landmarks ?? [], now);
    } catch (e) {
      console.error(e);
      setStatus(`検出に失敗: ${e.message}`, true);
    }
  };
  requestAnimationFrame(loop);
}

main();

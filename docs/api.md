# API

座標はすべて 0.0〜1.0 の正規化値。`rect` は `[x, y, w, h]` で左上原点。

## UnitV2 — http://unitv2.local:8080

### GET /
アプリ本体（static/index.html）。

### GET /stream
MJPEG (multipart/x-mixed-replace)。TimerCam の `/stream` をそのまま中継。
`--mock` 時は PC の内蔵カメラ。
同一オリジンで返すので CORS 設定は不要。

### POST /shoot
```json
{ "rect": [0.30, 0.25, 0.40, 0.30] }
```
→ 202
```json
{ "id": "20260911-143022-a3f", "status": "queued" }
```
即座に返し、撮影・切り出し・送信は非同期。
`rect` は TimerCam 座標。UnitV2 座標への変換は**スマホ側で済ませて送る**。

### GET /status
```json
{ "queue": 1, "last": { "id": "...", "state": "ok", "text": "紫陽花" } }
```
`state` は `queued` | `uploading` | `ok` | `error`。
`text` は Gemini が返した 8 文字以内の要約（Glass2 用）。未実装時は null。

### GET /config
```json
{ "sx": 0.45, "sy": 0.45, "ox": 0.0, "oy": 0.0 }
```
TimerCam 座標 → UnitV2 座標のスケールとオフセット。

### POST /config
同じ形を受けて `config.json` に保存。即時反映。

### POST /hud
スマホからの HUD 要求。UnitV2 は TimerCam の `/hud` にそのまま転送する。
ボディは TimerCam の `/hud` と同一。

---

## TimerCam — http://timercam.local

### GET /stream
MJPEG 640x480、jpeg_quality 12。

### POST /hud
```json
{ "slot": 0, "rect": [0.30, 0.25, 0.40, 0.30] }
{ "slot": 1, "text": "LOCK" }
{ "slot": 2, "text": "UP 3" }
```
→ 204

- slot 0: 指の枠（実線の矩形）
- slot 1: 状態。左上に表示。`SEEK` / `LOCK` / `3` `2` `1` / `HOLD`
- slot 2: 送信状況。右下に表示。UnitV2 が書く

3 スロットを保持し、**最大 10fps で合成して 1 回だけ描画**する。
各スロットに TTL 2 秒。期限切れは消す（書き手が落ちても固まらないように）。
`text` は 8 文字以内。超えたら切り捨てる。

Glass2 の固定表示として、UnitV2 が実際に撮れる範囲を点線の矩形で常時描く。
範囲は `/config` の `sx` `sy` `ox` `oy` から計算し、起動時に一度だけ受け取る。

---

## エンドポイントはこれで全部（UnitV2 7 本、TimerCam 2 本）
増やす場合はこのファイルを先に更新する。
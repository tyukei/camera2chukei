# Finger Camera

指で四角を作り、その枠の中を撮影して Google Drive に上げるウェアラブルカメラ。

## 構成

| 要素 | 実体 | 役割 |
|---|---|---|
| スマホ | ブラウザ (server/static) | MediaPipe で手を検出、枠を判定、`/shoot` を叩く |
| UnitV2 | Linux + Python (server/) | TimerCam の中継、1080p 撮影、切り出し、Drive 送信、Gemini |
| TimerCam | ESP32 (firmware/) | VGA 配信、Glass2 への HUD 描画 |
| Glass2 | 透明 OLED 128x64 | TimerCam に I2C 直結 (0x3C) |

**スマホは UnitV2 としか通信しない。** TimerCam への到達はすべて UnitV2 が中継する。

## ハード制約（重要）

- UnitV2: Cortex-A7 1.2GHz x2、**NPU なし、RAM 128MB**。重い推論を載せない。
  ML は「スマホ側の MediaPipe」か「Gemini API」のどちらか。UnitV2 では動かさない。
- TimerCam: ESP32、Grove は **SDA=G4 / SCL=G13**。Glass2 は I2C 0x3C。
- TimerCam の HUD 更新は **最大 10fps**。それ以上送ると配信が詰まる。
- 座標はすべて **0.0〜1.0 の正規化値**。ピクセルを API に載せない。

## 禁止事項

- WebSocket を使わない。すべて HTTP。HUD は POST の連打で足りる。
- ビルドステップを作らない。npm / webpack / TypeScript / バンドラ すべて不可。
  `static/` は素の HTML と JS。MediaPipe は CDN から読む。
- 依存を増やさない。Python 側は `fastapi` `uvicorn` `pillow` `requests` のみ。
- `api.md` に無いエンドポイントを足さない。必要なら先に api.md の変更を提案する。
- 1 ファイル 200 行を超えたら、分割案を出して承認を得てから分ける。
- 例外を握り潰さない。撮影・送信の失敗はキューに残してログに出す。

## 開発

```bash
# ハードなしで全部動く
python -m server.app --mock
# → /stream は PC の内蔵カメラ、/shoot は out/ に保存、/hud は stdout に print
open http://localhost:8080

# 実機
python -m server.app --timercam http://192.168.1.50
```

## 動作確認

```bash
curl -X POST localhost:8080/shoot -H 'Content-Type: application/json' \
  -d '{"rect":[0.3,0.3,0.4,0.3]}'
curl localhost:8080/status
```

## 実装順

1. `--mock` でサーバ起動、index.html が出る
2. MediaPipe で両手検出、canvas に枠を描く
3. ドウェル 1 秒で `/shoot`、`out/` に切り出し保存 ← **ここまでハード不要**
4. TimerCam ファーム、`/stream` を実機に
5. `/config` のスライダーで座標合わせ
6. Drive 送信
7. `/hud` と Glass2
8. Gemini タグ付け

各ステップが終わったら動作確認してから次に進む。先の工程のコードを先回りして書かない。

## 用語

- **枠 / rect**: 指で作った四角。`[x, y, w, h]` の正規化値、TimerCam 画像座標系。
- **ドウェル**: 枠が一定時間静止したらシャッターを切る判定。
- **スロット**: Glass2 の表示領域。0=枠、1=状態、2=送信状況。
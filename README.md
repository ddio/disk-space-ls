# disk-space-ls (dsls)

掃描家目錄與 Docker，列出最佔空間的目錄，並用條件式規則給出可清理建議。
只產生報告、**不會主動刪任何東西**——每個建議都附上可直接複製執行的指令與風險等級。

## 用法

```bash
dsls                    # 增量掃描 $HOME + Docker,輸出報告
dsls --full             # 忽略所有快取,完整重掃 (建議每週跑一次校正)
dsls --json             # 機器可讀輸出
dsls --path ~/src       # 掃指定目錄 (清理建議僅在掃 $HOME 時產生)
dsls --depth 3          # 目錄樹顯示深度 (預設 2)
dsls --min-size 500     # 目錄樹只顯示 >500MB 的目錄 (預設 100)
dsls --stale-days 30    # 專案幾天沒動視為停滯 (預設 90)
dsls --top-images 30    # Docker image 清單顯示數量,0=全部 (預設 15)
dsls --exclude 'SynologyDrive' --exclude '*.git'   # 排除 glob
dsls --no-docker --no-system                       # 只掃檔案
dsls --clear-cache      # 清掉快取
```

## 速度設計

| 情境 | 耗時 (31 萬個目錄實測) |
|---|---|
| 第一次完整掃描 | ~5 分鐘 |
| 之後增量掃描 | **4–6 秒** |

- **目錄樹快取**（`~/.cache/disk-space-ls/tree-cache.json`）：記錄每個目錄的
  mtime、直屬檔案大小、子目錄清單。目錄 mtime 沒變就整包沿用，只 stat 目錄本身。
- **Docker**：每次執行只跑毫秒級的 metadata 查詢（image 清單、container/volume 數量、
  `docker builder du`）。很慢的 `docker system df`（要即時 du 所有 container rw layer
  與 volume，實測近 2 分鐘）改由 detached 背景行程更新，結果快取 12 小時，主程式
  **從不等它**——快取還沒好時，container/volume 的精確大小顯示「背景計算中」，下次執行
  補上。只有 `--full` 會同步等 df 跑完。
- 已知限制：檔案「原地變大」（如 log append）不會改變目錄 mtime，要等該目錄有
  增刪檔案、或跑 `--full` 才會反映。所以建議定期 `--full` 一次。

其他行為：

- 大小算的是實際磁碟用量（`st_blocks`，在 ZFS 上反映壓縮後大小），不是表面檔案大小。
- 自動跳過網路/雲端掛載（fuse/nfs/cifs/sshfs…，如 pCloudDrive），報告會列出跳過了哪些。

## 清理建議規則（全部條件式，不用 LLM）

| 類別 | 條件 | 風險 |
|---|---|---|
| 垃圾桶 | `~/.local/share/Trash` 有東西 | 低 |
| 已知快取 | pip / npm / pnpm / yarn / poetry / uv / go / cargo / gradle / maven / HuggingFace / 瀏覽器快取 / 縮圖… >50MB | 低 |
| `~/.cache` 其他 | 未列入上表但 >200MB 的直屬子目錄 | 低 |
| 停滯專案建置產物 | `node_modules` / `venv` / `.venv` / `target` / `.next` 等，**必須在 git repo 內**且有對應標記檔（package.json…），整個專案超過 `--stale-days` 沒動 | 低 |
| 下載區舊大檔 | `~/Downloads`、`~/下載` 中 >100MB 且超過 180 天 | 中 |
| 家目錄大 log | `~/*.log` >20MB | 中 |
| Docker | dangling image（低）、unused image（中）、build cache `docker builder prune -a -f`（低，含還在用的層，下次 build 慢一次）、停止的容器（中）、未掛載 volume（**高**，可能有資料庫資料） | 低–高 |
| Docker image 清單 | 依大小列出各 image、建立時間、是否有容器在用（`--top-images` 控制數量） | — |
| 系統 | journald >500MB、apt cache >100MB、停用的舊版 snap | 低 |

「必須在 git repo 內」是為了避免誤刪 VS Code extension、`.nvm`、安裝版 app
內部的 `node_modules`。

## 定期執行

```bash
crontab -e
# 每天早上 9 點增量掃描,每週一改跑完整掃描
0 9 * * 2-7 ~/bin/dsls --no-docker > ~/.cache/disk-space-ls/last-report.txt 2>/dev/null
0 9 * * 1   ~/bin/dsls --full      > ~/.cache/disk-space-ls/last-report.txt 2>/dev/null
```

搭配 `--json` 可以再接告警（例如剩餘空間 <10% 時發通知）。

## 安裝

```bash
ln -sf ~/src/disk-space-ls/dsls.py ~/bin/dsls
```

純 Python 3 標準函式庫，無任何相依套件。

#!/usr/bin/env python3
"""disk-space-ls (dsls) — 掃描家目錄與 Docker,找出佔空間的目錄並給出可清理建議。

設計重點:
- 第一次掃描會走訪全部檔案;之後利用「目錄 mtime 快取」只重掃有變動的目錄,
  沒動過的子樹直接沿用快取,速度快非常多。
- 只產生報告,不會主動刪除任何東西;每個建議都附上可直接執行的清理指令。
- 已知限制:檔案「原地變大」(如 log append) 不會改變目錄 mtime,快取要等目錄
  有增刪檔案才會更新;建議定期 (如每週) 用 --full 跑一次完整掃描校正。

用法:
    dsls.py                  # 掃描 $HOME + docker,輸出報告
    dsls.py --full           # 忽略快取,完整重掃
    dsls.py --json           # 機器可讀輸出
    dsls.py --path DIR       # 掃指定目錄
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "disk-space-ls"
)
CACHE_FILE = os.path.join(CACHE_DIR, "tree-cache.json")
CACHE_VERSION = 1

# 這些檔案系統型別視為網路/雲端掛載,預設不掃 (pCloud、NFS、SMB...)
SKIP_FSTYPES = ("fuse", "nfs", "cifs", "smb", "sshfs", "davfs", "afs", "9p")

# 快取 entry: [dir_mtime, files_size, file_count, files_newest_mtime, [subdir names]]
M_MTIME, M_SIZE, M_COUNT, M_NEWEST, M_SUBS = range(5)


def human(n):
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n:.0f}B"
        n /= 1024
    return f"{n:.1f}T"


def parse_docker_size(s):
    """把 docker 的 '6.219GB (78%)' / '1.5kB' 轉成 bytes。"""
    m = re.match(r"([\d.]+)\s*([kKMGT]?i?B)", s or "")
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower().rstrip("b").rstrip("i")
    mult = {"": 1, "k": 1000, "m": 1000**2, "g": 1000**3, "t": 1000**4}[unit]
    return int(val * mult)


def network_mounts_under(root):
    skipped = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt, fstype = parts[1], parts[2]
                mnt = mnt.encode().decode("unicode_escape")  # \040 等跳脫
                if mnt.startswith(root + os.sep) and fstype.startswith(SKIP_FSTYPES):
                    skipped.add(mnt)
    except OSError:
        pass
    return skipped


class Scanner:
    def __init__(self, root, cache, excludes, full=False, progress=True):
        self.root = root
        self.cache = {} if full else cache
        self.new_cache = {}
        self.sizes = {}      # path -> subtree bytes (實際磁碟用量, st_blocks)
        self.newest = {}     # path -> subtree 最新 mtime
        self.excludes = excludes
        self.skipped_mounts = network_mounts_under(root)
        self.dirs_seen = 0
        self.files_seen = 0
        self.cache_hits = 0
        self.errors = 0
        self.progress = progress and sys.stderr.isatty()

    def excluded(self, path, name):
        if path in self.skipped_mounts:
            return True
        for pat in self.excludes:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat):
                return True
        return False

    def scan(self):
        sys.setrecursionlimit(30000)
        total, count, newest = self._scan_dir(self.root)
        if self.progress:
            sys.stderr.write("\r\033[K")
        return total

    def _scan_dir(self, path):
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            self.errors += 1
            return 0, 0, 0
        self.dirs_seen += 1
        if self.progress and self.dirs_seen % 2000 == 0:
            sys.stderr.write(
                f"\r掃描中... {self.dirs_seen} 個目錄 (快取命中 {self.cache_hits})"
            )
            sys.stderr.flush()

        ent = self.cache.get(path)
        if ent and ent[M_MTIME] == st.st_mtime:
            self.cache_hits += 1
            files_size = ent[M_SIZE]
            file_count = ent[M_COUNT]
            files_newest = ent[M_NEWEST]
            subdirs = ent[M_SUBS]
        else:
            files_size = st.st_blocks * 512  # 目錄本身佔的空間
            file_count = 0
            files_newest = st.st_mtime
            subdirs = []
            try:
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                subdirs.append(e.name)
                            elif e.is_file(follow_symlinks=False):
                                est = e.stat(follow_symlinks=False)
                                files_size += est.st_blocks * 512
                                file_count += 1
                                if est.st_mtime > files_newest:
                                    files_newest = est.st_mtime
                        except OSError:
                            self.errors += 1
            except OSError:
                self.errors += 1
                return 0, 0, 0

        total, count, newest = files_size, file_count, files_newest
        for name in subdirs:
            sub = os.path.join(path, name)
            if self.excluded(sub, name):
                continue
            s, c, n = self._scan_dir(sub)
            total += s
            count += c
            newest = max(newest, n)

        self.new_cache[path] = [st.st_mtime, files_size, file_count, files_newest, subdirs]
        self.sizes[path] = total
        self.newest[path] = newest
        self.files_seen += count if path == self.root else 0
        return total, count, newest


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        if data.get("version") == CACHE_VERSION:
            return data.get("dirs", {})
    except (OSError, ValueError):
        pass
    return {}


def save_cache(old, scanner):
    os.makedirs(CACHE_DIR, exist_ok=True)
    root = scanner.root
    merged = {
        p: v
        for p, v in old.items()
        if not (p == root or p.startswith(root + os.sep))
    }
    merged.update(scanner.new_cache)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": CACHE_VERSION, "saved_at": time.time(), "dirs": merged},
                  f, separators=(",", ":"))
    os.replace(tmp, CACHE_FILE)


# ---------------------------------------------------------------- 建議規則

RISK_LABEL = {"low": "低", "medium": "中", "high": "高"}

# 可重建的快取目錄: (相對 home 的路徑, 說明, 清理指令或 None=rm -rf)
KNOWN_CACHES = [
    (".cache/pip", "pip 下載快取,可重新下載", "pip cache purge"),
    (".cache/uv", "uv 快取", "uv cache clean"),
    (".cache/pypoetry", "poetry 快取", None),
    (".cache/yarn", "yarn 快取", "yarn cache clean"),
    (".npm", "npm 快取", "npm cache clean --force"),
    (".local/share/pnpm/store", "pnpm store", "pnpm store prune"),
    (".cache/pnpm", "pnpm 快取", None),
    (".cache/go-build", "go build 快取", "go clean -cache"),
    (".cargo/registry", "cargo registry 快取,可重新下載", None),
    (".gradle/caches", "gradle 快取", None),
    (".m2/repository", "maven 本地 repo,可重新下載", None),
    (".cache/ccache", "ccache", "ccache -C"),
    (".cache/huggingface", "HuggingFace 模型快取 (刪了要重新下載模型)", None),
    (".cache/torch", "PyTorch 模型快取", None),
    (".cache/composer", "composer 快取", "composer clear-cache"),
    (".cache/google-chrome", "Chrome 快取,重開會重建", None),
    (".cache/chromium", "Chromium 快取", None),
    (".cache/mozilla", "Firefox 快取,重開會重建", None),
    (".cache/thumbnails", "縮圖快取,會自動重建", None),
]

# 專案建置產物: 目錄名 -> (說明, 專案標記檔)
ARTIFACT_DIRS = {
    "node_modules": ("npm/yarn/pnpm install 可重建", ["package.json"]),
    ".venv": ("python venv,可重建", ["pyproject.toml", "requirements.txt", "setup.py", "Pipfile"]),
    "venv": ("python venv,可重建", ["pyproject.toml", "requirements.txt", "setup.py", "Pipfile"]),
    ".tox": ("tox 環境,可重建", ["tox.ini", "pyproject.toml"]),
    "target": ("cargo build 產物,可重建", ["Cargo.toml"]),
    ".next": ("next.js build 產物", ["package.json"]),
    ".nuxt": ("nuxt build 產物", ["package.json"]),
    ".pytest_cache": ("pytest 快取", ["pyproject.toml", "setup.py", "requirements.txt"]),
    ".mypy_cache": ("mypy 快取", ["pyproject.toml", "setup.py", "requirements.txt"]),
}


def add(suggestions, category, path, size, reason, command, risk):
    suggestions.append({
        "category": category,
        "path": path,
        "size": size,
        "reason": reason,
        "command": command,
        "risk": risk,
    })


def suggest_home(scanner, args):
    s = []
    sizes = scanner.sizes
    now = time.time()
    stale_secs = args.stale_days * 86400

    def sz(p):
        return sizes.get(p)

    # 垃圾桶
    trash = os.path.join(HOME, ".local/share/Trash")
    if sz(trash) and sz(trash) > 1 << 20:
        add(s, "垃圾桶", trash, sz(trash), "垃圾桶內容",
            "gio trash --empty", "low")

    # 已知快取
    covered = set()
    for rel, reason, cmd in KNOWN_CACHES:
        p = os.path.join(HOME, rel)
        size = sz(p)
        if size and size > 50 << 20:
            covered.add(p)
            add(s, "快取", p, size, reason, cmd or f"rm -rf '{p}'", "low")

    # ~/.cache 底下其他大目錄
    cache_root = os.path.join(HOME, ".cache")
    for name in scanner.new_cache.get(cache_root, [0, 0, 0, 0, []])[M_SUBS]:
        p = os.path.join(cache_root, name)
        if p in covered or any(p == c or c.startswith(p + os.sep) for c in covered):
            continue
        size = sz(p)
        if size and size > 200 << 20:
            add(s, "快取", p, size, "~/.cache 下的大目錄,通常可刪 (應用程式會重建)",
                f"rm -rf '{p}'", "low")

    # 停滯專案的建置產物 (node_modules / venv / target ...)
    # 只認「在 git repo 裡」的,避免誤判 .vscode extension、.nvm、安裝版 app 內的 node_modules
    def in_git_repo(d):
        while d.startswith(scanner.root) and len(d) >= len(scanner.root):
            if os.path.isdir(os.path.join(d, ".git")):
                return True
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
        return False

    candidates = []
    for p in sizes:
        name = os.path.basename(p)
        if name not in ARTIFACT_DIRS:
            continue
        reason, markers = ARTIFACT_DIRS[name]
        parent = os.path.dirname(p)
        if not any(os.path.exists(os.path.join(parent, m)) for m in markers):
            continue
        if sizes[p] < 20 << 20:
            continue
        if not in_git_repo(parent):
            continue
        candidates.append(p)
    # 去掉巢狀 (node_modules 裡的 node_modules)
    candidates.sort()
    picked = []
    for p in candidates:
        if any(p.startswith(q + os.sep) for q in picked):
            continue
        picked.append(p)
    for p in picked:
        parent = os.path.dirname(p)
        newest = scanner.newest.get(parent, now)
        idle_days = int((now - newest) / 86400)
        if now - newest < stale_secs:
            continue
        reason, _ = ARTIFACT_DIRS[os.path.basename(p)]
        add(s, "停滯專案建置產物", p, sizes[p],
            f"專案 {parent} 已約 {idle_days} 天沒動;{reason}",
            f"rm -rf '{p}'", "low")

    # Downloads 裡的大型舊檔
    for dl in ("Downloads", "下載"):
        droot = os.path.join(HOME, dl)
        if not os.path.isdir(droot):
            continue
        for dirpath, dirnames, filenames in os.walk(droot, onerror=lambda e: None):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if st.st_blocks * 512 > 100 << 20 and now - st.st_mtime > 180 * 86400:
                    add(s, "下載區舊大檔", fp, st.st_blocks * 512,
                        f"超過 180 天的大檔 (自行確認後刪除)",
                        f"rm '{fp}'", "medium")

    # 家目錄第一層的大 log
    try:
        with os.scandir(HOME) as it:
            for e in it:
                if e.is_file(follow_symlinks=False) and e.name.endswith(".log"):
                    st = e.stat()
                    if st.st_blocks * 512 > 20 << 20:
                        add(s, "Log 檔", e.path, st.st_blocks * 512,
                            "家目錄下的大 log 檔", f"truncate -s 0 '{e.path}'", "medium")
    except OSError:
        pass

    return s


DOCKER_CACHE_FILE = os.path.join(CACHE_DIR, "docker-df.json")
DOCKER_CACHE_TTL = 12 * 3600  # docker system df 很慢,結果快取 12 小時
DOCKER_REFRESH_MARKER = DOCKER_CACHE_FILE + ".refreshing"


def _docker_run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _query_df():
    """docker system df — 會即時 du 所有 container rw layer 與 volume,常跑數分鐘。
    只在 --full 或背景更新時呼叫,一般執行只用它的快取。"""
    try:
        out = _docker_run(["docker", "system", "df", "--format", "{{json .}}"], 600)
        if out.returncode != 0:
            return None
        rows = {}
        for line in out.stdout.splitlines():
            try:
                row = json.loads(line)
                rows[row.get("Type", "?")] = row
            except ValueError:
                pass
        return rows or None
    except Exception:
        return None


def _load_df_cache():
    try:
        with open(DOCKER_CACHE_FILE) as f:
            cached = json.load(f)
        if time.time() - cached["at"] < DOCKER_CACHE_TTL:
            return cached["df"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _save_df_cache(rows):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = DOCKER_CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "df": rows}, f)
    os.replace(tmp, DOCKER_CACHE_FILE)


def _spawn_df_refresh():
    """另起 detached 行程更新 df 快取,本次執行不等它。"""
    try:
        if time.time() - os.path.getmtime(DOCKER_REFRESH_MARKER) < 15 * 60:
            return  # 已有一個背景更新在跑
    except OSError:
        pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(DOCKER_REFRESH_MARKER, "w") as f:
        f.write(str(time.time()))
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--docker-df-refresh"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def suggest_docker(force=False):
    s, info = _query_docker_fast()
    if "error" in info:
        return s, info
    df = None if force else _load_df_cache()
    if df is None and force:
        df = _query_df()  # --full 明確要求完整,同步等
        if df:
            _save_df_cache(df)
    if df:
        info.update(df)
    else:
        info["df_pending"] = True
        _spawn_df_refresh()
    _suggest_docker_items(s, info)
    return s, info


def _query_docker_fast():
    """只讀 metadata 的查詢,全部毫秒級;不碰要 du 磁碟的 docker system df。"""
    s = []
    info = {}
    try:
        ps = _docker_run(["docker", "ps", "-aq"], 30)
        if ps.returncode != 0:
            return s, {"error": ps.stderr.strip()[:200] or "docker 無法使用"}
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return s, {"error": f"docker 無法使用: {e.__class__.__name__}"}
    ids = ps.stdout.split()
    info["containers_total"] = len(ids)
    try:
        info["containers_running"] = len(_docker_run(["docker", "ps", "-q"], 30).stdout.split())
    except Exception:
        info["containers_running"] = None

    # image 清單。先查哪些 image 有容器在用
    used = set()
    images = []
    try:
        if ids:
            insp = _docker_run(["docker", "inspect", "-f", "{{.Image}}", *ids], 30)
            used = {l.strip().removeprefix("sha256:")[:12]
                    for l in insp.stdout.splitlines() if l.strip()}
        out = _docker_run(["docker", "images", "--format", "{{json .}}"], 60)
        for line in out.stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            images.append({
                "repo": row.get("Repository", "<none>"),
                "tag": row.get("Tag", "<none>"),
                "id": row.get("ID", ""),
                "size": parse_docker_size(row.get("Size", "")),
                "size_h": row.get("Size", "?"),
                "created": row.get("CreatedSince", "?"),
                "in_use": row.get("ID", "")[:12] in used,
            })
        images.sort(key=lambda i: -(i["size"] or 0))
    except Exception:
        pass
    info["images"] = images

    # volume 數量 (大小要靠 df,這裡只數)
    try:
        info["volumes_total"] = len(_docker_run(["docker", "volume", "ls", "-q"], 30).stdout.split())
        info["volumes_dangling"] = len(
            _docker_run(["docker", "volume", "ls", "-f", "dangling=true", "-q"], 30).stdout.split())
    except Exception:
        info["volumes_total"] = info["volumes_dangling"] = None

    # build cache 總量: docker builder du 只讀 buildkit 記錄,不 du 磁碟
    info["build_cache_bytes"] = None
    try:
        out = _docker_run(["docker", "builder", "du"], 60)
        m = re.search(r"^Total:\s*([\d.]+\s*[kKMGT]?i?B)", out.stdout, re.M)
        if m:
            info["build_cache_bytes"] = parse_docker_size(m.group(1))
    except Exception:
        pass

    # dangling image (沒 tag 的中間層) — 安全
    try:
        out = _docker_run(["docker", "images", "-f", "dangling=true", "-q"], 30)
        info["n_dangling"] = len([l for l in out.stdout.splitlines() if l.strip()])
    except Exception:
        info["n_dangling"] = 0
    return s, info


def _suggest_docker_items(s, info):
    """由快速查詢 (必要時輔以 df 快取) 產生建議。"""
    def reclaimable(t):
        row = info.get(t)
        return parse_docker_size(row.get("Reclaimable", "")) if row else None

    if info.get("n_dangling"):
        add(s, "Docker", "dangling images", None,
            f"{info['n_dangling']} 個沒有 tag 的 image 層", "docker image prune -f", "low")

    r = reclaimable("Images")
    approx = ""
    if r is None:
        r = sum(im["size"] or 0 for im in info.get("images", []) if not im["in_use"])
        approx = " (估計值,image 間共用 layer 時實際較少)"
    if r and r > 100 << 20:
        add(s, "Docker", "unused images", r,
            "沒有任何容器在用的 image (需要時可重新 pull/build)" + approx,
            "docker image prune -a", "medium")

    # df 的 Build Cache Reclaimable 常是 0B (只算 dangling 的);-a 可清掉全部
    r = info.get("build_cache_bytes") or reclaimable("Build Cache")
    if r and r > 100 << 20:
        add(s, "Docker", "build cache", r,
            "build cache 可重建;-a 連還在用的層一起清,下次 build 會慢一次",
            "docker builder prune -a -f", "low")

    r = reclaimable("Containers")
    n_stopped = (info.get("containers_total") or 0) - (info.get("containers_running") or 0)
    if (r and r > 50 << 20) or (r is None and n_stopped > 0):
        add(s, "Docker", "stopped containers", r,
            f"{n_stopped} 個已停止的容器 (確認沒有要保留的資料/設定再刪)",
            "docker container prune", "medium")

    r = reclaimable("Local Volumes")
    nd = info.get("volumes_dangling")
    if (r and r > 50 << 20) or (r is None and nd):
        add(s, "Docker", "unused volumes", r,
            f"{nd if nd is not None else '?'} 個沒被任何容器掛載的 volume"
            " — 裡面可能有資料庫等資料,務必確認!",
            "docker volume ls -f dangling=true  # 先看清單再 docker volume prune",
            "high")


def suggest_system():
    s = []
    # journald
    try:
        out = subprocess.run(["journalctl", "--disk-usage"],
                             capture_output=True, text=True, timeout=15)
        m = re.search(r"([\d.]+[KMGT]?i?B)", out.stdout)
        size = parse_docker_size(m.group(1).replace("iB", "B")) if m else None
        if size and size > 500 << 20:
            add(s, "系統", "systemd journal", size, "系統日誌",
                "sudo journalctl --vacuum-size=200M", "low")
    except Exception:
        pass
    # apt cache
    apt = "/var/cache/apt/archives"
    try:
        total = sum(
            e.stat().st_blocks * 512
            for e in os.scandir(apt)
            if e.is_file() and e.name.endswith(".deb")
        )
        if total > 100 << 20:
            add(s, "系統", apt, total, "apt 下載的 .deb 快取", "sudo apt clean", "low")
    except OSError:
        pass
    # snap 舊版本
    try:
        out = subprocess.run(["snap", "list", "--all"],
                             capture_output=True, text=True, timeout=15)
        disabled = [l.split()[0] for l in out.stdout.splitlines() if "disabled" in l]
        if disabled:
            add(s, "系統", "snap 舊版本", None,
                f"{len(disabled)} 個停用的舊版 snap: {', '.join(sorted(set(disabled))[:6])}...",
                "snap list --all | awk '/disabled/{print $1, $3}' | "
                "while read n r; do sudo snap remove \"$n\" --revision=\"$r\"; done",
                "low")
    except Exception:
        pass
    return s


# ---------------------------------------------------------------- 報告輸出

def print_tree(scanner, args):
    sizes = scanner.sizes
    root = scanner.root
    min_size = args.min_size << 20

    def walk(path, depth):
        if depth > args.depth:
            return
        subs = scanner.new_cache.get(path, [0, 0, 0, 0, []])[M_SUBS]
        children = sorted(
            ((sizes.get(os.path.join(path, n), 0), n) for n in subs),
            reverse=True,
        )
        for size, name in children:
            if size < min_size:
                continue
            print(f"  {human(size):>8}  {'    ' * depth}{name}/")
            walk(os.path.join(path, name), depth + 1)

    print(f"  {human(sizes.get(root, 0)):>8}  {root}")
    walk(root, 1)


def print_suggestions(all_s):
    if not all_s:
        print("  (沒有明顯可清的東西)")
        return
    by_cat = {}
    for item in all_s:
        by_cat.setdefault(item["category"], []).append(item)
    total_low = 0
    for cat, items in by_cat.items():
        print(f"\n  ── {cat} ──")
        for it in sorted(items, key=lambda x: -(x["size"] or 0)):
            risk = RISK_LABEL[it["risk"]]
            size = human(it["size"]) if it["size"] else ""
            print(f"  [風險:{risk}] {size:>8}  {it['path']}")
            print(f"             {it['reason']}")
            print(f"             $ {it['command']}")
            if it["risk"] == "low" and it["size"]:
                total_low += it["size"]
    print(f"\n  低風險項目合計約可釋出: {human(total_low)}")


def main():
    ap = argparse.ArgumentParser(description="掃描磁碟空間並建議可清理項目")
    ap.add_argument("--path", default=HOME, help="掃描根目錄 (預設 $HOME)")
    ap.add_argument("--full", action="store_true", help="忽略快取,完整重掃")
    ap.add_argument("--clear-cache", action="store_true", help="清掉快取後結束")
    ap.add_argument("--depth", type=int, default=2, help="目錄樹顯示深度 (預設 2)")
    ap.add_argument("--min-size", type=int, default=100,
                    help="目錄樹只顯示大於此 MB 的目錄 (預設 100)")
    ap.add_argument("--stale-days", type=int, default=90,
                    help="專案幾天沒動視為停滯 (預設 90)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="排除的 glob pattern,可重複")
    ap.add_argument("--top-images", type=int, default=15,
                    help="Docker image 清單顯示數量,0=全部 (預設 15)")
    ap.add_argument("--no-docker", action="store_true")
    ap.add_argument("--no-system", action="store_true")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--docker-df-refresh", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.docker_df_refresh:  # 內部用: 背景更新 docker system df 快取
        rows = _query_df()
        if rows:
            _save_df_cache(rows)
        try:
            os.remove(DOCKER_REFRESH_MARKER)
        except OSError:
            pass
        return

    if args.clear_cache:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print("快取已清除")
        return

    root = os.path.realpath(args.path)
    t0 = time.time()

    # docker 查詢與檔案掃描平行;一般執行只跑毫秒級 metadata 查詢,
    # 慢的 docker system df 由 detached 背景行程更新快取,這裡不等它
    docker_result = {}
    docker_thread = None
    if not args.no_docker:
        def run_docker():
            docker_result["s"], docker_result["info"] = suggest_docker(force=args.full)
        docker_thread = threading.Thread(target=run_docker, daemon=True)
        docker_thread.start()

    old_cache = load_cache()
    scanner = Scanner(root, old_cache, args.exclude, full=args.full,
                      progress=not args.json)
    total = scanner.scan()
    scan_secs = time.time() - t0
    save_cache(old_cache, scanner)

    suggestions = suggest_home(scanner, args) if root == HOME else []
    docker_info = {}
    if docker_thread:
        if docker_thread.is_alive() and sys.stderr.isatty() and not args.json:
            sys.stderr.write("等待 docker 查詢...\r")
            sys.stderr.flush()
        # --full 會同步跑 docker system df (可能數分鐘),其餘只是 metadata 查詢
        docker_thread.join(timeout=660 if args.full else 120)
        if sys.stderr.isatty() and not args.json:
            sys.stderr.write("\033[K")
        if docker_result:
            suggestions += docker_result.get("s", [])
            docker_info = docker_result.get("info", {})
        else:
            docker_info = {"error": "docker 查詢逾時,略過"}
    if not args.no_system:
        suggestions += suggest_system()

    if args.json:
        top = sorted(scanner.sizes.items(), key=lambda kv: -kv[1])[:200]
        print(json.dumps({
            "root": root, "total_bytes": total, "scan_seconds": round(scan_secs, 2),
            "dirs_scanned": scanner.dirs_seen, "cache_hits": scanner.cache_hits,
            "skipped_mounts": sorted(scanner.skipped_mounts),
            "top_dirs": [{"path": p, "bytes": b} for p, b in top],
            "suggestions": suggestions,
            "docker": docker_info,
        }, ensure_ascii=False, indent=1))
        return

    df = shutil.disk_usage(root)
    print(f"== 磁碟總覽 ==")
    print(f"  {root} 佔用 {human(total)};所在檔案系統 "
          f"{human(df.used)} / {human(df.total)} ({df.used / df.total:.0%})")
    print(f"  掃描 {scanner.dirs_seen} 個目錄,快取命中 {scanner.cache_hits},"
          f"耗時 {scan_secs:.1f}s"
          + (f",錯誤 {scanner.errors}" if scanner.errors else ""))
    if scanner.skipped_mounts:
        print(f"  已跳過網路/雲端掛載: {', '.join(sorted(scanner.skipped_mounts))}")

    print(f"\n== 最佔空間的目錄 (深度 {args.depth},>{args.min_size}MB) ==")
    print_tree(scanner, args)

    if docker_info and "error" not in docker_info:
        print(f"\n== Docker ==")
        for t in ("Images", "Containers", "Local Volumes", "Build Cache"):
            row = docker_info.get(t)
            if row:
                print(f"  {t:<14} {row.get('TotalCount', '?'):>4} 個, "
                      f"共 {row.get('Size', '?'):>10}, "
                      f"可回收 {row.get('Reclaimable', '?')}")
        if docker_info.get("df_pending"):
            ct = docker_info.get("containers_total")
            cr = docker_info.get("containers_running") or 0
            vt = docker_info.get("volumes_total")
            vd = docker_info.get("volumes_dangling")
            print(f"  Images         {len(docker_info.get('images') or []):>4} 個")
            if ct is not None:
                print(f"  Containers     {ct:>4} 個 (其中 {ct - cr} 個已停止)")
            if vt is not None:
                print(f"  Local Volumes  {vt:>4} 個 (其中 {vd} 個未掛載)")
            if docker_info.get("build_cache_bytes") is not None:
                print(f"  Build Cache    共 {human(docker_info['build_cache_bytes'])}")
            print("  (精確大小由 docker system df 在背景計算,下次執行會顯示)")
        imgs = docker_info.get("images") or []
        if imgs:
            n = args.top_images if args.top_images > 0 else len(imgs)
            unused = sum(1 for im in imgs if not im["in_use"])
            print(f"\n  Image 清單 (依大小,前 {min(n, len(imgs))} 個;"
                  f"共 {len(imgs)} 個,其中 {unused} 個沒有容器在用):")
            for im in imgs[:n]:
                mark = "使用中" if im["in_use"] else "未使用"
                print(f"    {im['size_h']:>10}  [{mark}]  "
                      f"{im['repo']}:{im['tag']}  ({im['created']})")
            if len(imgs) > n:
                print(f"    ... 還有 {len(imgs) - n} 個,完整清單: docker images")
            print("    (image 之間會共用 layer,大小加總會大於實際磁碟用量)")
    elif docker_info.get("error"):
        print(f"\n== Docker ==\n  ({docker_info['error']})")

    print(f"\n== 清理建議 ==")
    print_suggestions(suggestions)


if __name__ == "__main__":
    main()

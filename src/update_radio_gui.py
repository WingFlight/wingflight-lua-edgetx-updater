#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rotorflight Lua EdgeTX/OpenTX Updater
=====================================
A GUI tool to update Rotorflight Lua scripts on an EdgeTX/OpenTX SD card.

Features:
- Detects a mounted SD card from its folder layout
- Downloads release, snapshot, or development builds from GitHub
- Updates SCRIPTS and WIDGETS on the SD card
"""

import atexit
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:
    print("Error: tkinter is required but not found.")
    sys.exit(1)


GITHUB_REPO_URL = "https://github.com/rotorflight/rotorflight-lua-scripts"
GITHUB_API_URL = "https://api.github.com/repos/rotorflight/rotorflight-lua-scripts"
UPDATER_INFO_URL = "https://github.com/rotorflight/rotorflight-lua-edgetx-updater/releases"
LOGO_URL = "https://raw.githubusercontent.com/rotorflight/rotorflight-lua-edgetx-updater/master/src/logo.png"

INSTALL_DIR_NAMES = ("SCRIPTS", "WIDGETS")
RADIO_ROOT_HINTS = (
    "IMAGES",
    "LOGS",
    "MODELS",
    "RADIO",
    "SCREENSHOTS",
    "SOUNDS",
    "TEMPLATES",
    "THEMES",
    "WIDGETS",
)
AUTO_DETECT_MIN_HINTS = 3

VERSION_RELEASE = "release"
VERSION_SNAPSHOT = "snapshot"
VERSION_MASTER = "master"

DOWNLOAD_TIMEOUT = 120
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 2
COPY_SETTLE_SECONDS = 0.03
TS_SLACK_SECONDS = 2.0
CACHE_DIRNAME = "cache"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _get_app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _get_resource_dir():
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _get_app_dir()
RESOURCE_DIR = _get_resource_dir()


def _get_work_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "rotorflight-edgetx-updater"
    if sys.platform.startswith("linux"):
        return Path.home() / ".local" / "share" / "rotorflight-edgetx-updater"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
        return base / "rotorflight_edgetx_updater_work"
    return APP_DIR / "rotorflight_edgetx_updater_work"


WORK_DIR = _get_work_dir()
try:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    WORK_DIR = Path(tempfile.gettempdir()) / "rotorflight_edgetx_updater_work"
    WORK_DIR.mkdir(parents=True, exist_ok=True)

UPDATER_SETTINGS_FILE = str(WORK_DIR / "updater_settings.json")
UPDATER_LOCK_FILE = str(WORK_DIR / "rotorflight_edgetx_updater.lock")


def _ensure_work_dir():
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _pid_is_running(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], universal_newlines=True)
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _clear_stale_lock_file():
    if not os.path.exists(UPDATER_LOCK_FILE):
        return
    pid_str = ""
    try:
        with open(UPDATER_LOCK_FILE, "r", encoding="utf-8") as f:
            pid_str = f.read().strip()
    except Exception:
        pass
    if not pid_str or not _pid_is_running(pid_str):
        try:
            os.remove(UPDATER_LOCK_FILE)
        except Exception:
            pass


def _clear_cache_dir():
    cache_dir = WORK_DIR / CACHE_DIRNAME
    if not cache_dir.exists():
        return True, None
    if not cache_dir.is_dir():
        return False, f"Cache path is not a directory: {cache_dir}"

    def _on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, 0o700)
            func(path)
            return
        except Exception:
            raise exc_info[1]

    try:
        shutil.rmtree(cache_dir, onerror=_on_rm_error)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class RadioStorageInterface:
    """Locate a mounted EdgeTX/OpenTX SD card."""

    def __init__(self, log_cb=None):
        self.log_cb = log_cb

    def _log(self, message):
        if self.log_cb:
            self.log_cb(message)

    def _scripts_dir_in_root(self, root):
        for name in ("SCRIPTS", "scripts"):
            path = os.path.join(root, name)
            if os.path.isdir(path):
                return os.path.normpath(path)
        return None

    def _hint_count(self, root):
        return sum(1 for hint in RADIO_ROOT_HINTS if os.path.isdir(os.path.join(root, hint)))

    def _is_edge_tx_root(self, root):
        return bool(self._scripts_dir_in_root(root)) and self._hint_count(root) >= AUTO_DETECT_MIN_HINTS

    def _score_root(self, root):
        if not os.path.isdir(root):
            return -1
        scripts_dir = self._scripts_dir_in_root(root)
        if not scripts_dir:
            return -1

        hint_count = self._hint_count(root)
        if hint_count < AUTO_DETECT_MIN_HINTS:
            return -1

        score = 100 + hint_count
        if os.path.isdir(os.path.join(scripts_dir, "TOOLS")):
            score += 4
        if os.path.isdir(os.path.join(scripts_dir, "FUNCTIONS")):
            score += 4
        if os.path.isdir(os.path.join(root, "WIDGETS")):
            score += 2
        return score

    def _iter_mount_roots(self):
        for base in ("/Volumes", "/media", "/mnt", "/run/media"):
            if not os.path.isdir(base):
                continue
            try:
                for entry in os.scandir(base):
                    if not entry.is_dir():
                        continue
                    if base in ("/run/media", "/media"):
                        try:
                            for sub in os.scandir(entry.path):
                                if sub.is_dir():
                                    yield sub.path
                        except Exception:
                            pass
                    yield entry.path
            except Exception:
                continue

    def _iter_lsblk_mounts(self):
        if sys.platform == "darwin":
            return
        try:
            result = subprocess.run(
                ["lsblk", "-o", "NAME,TRAN,RM,MOUNTPOINT", "-P"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not result.stdout:
                return
            for line in result.stdout.splitlines():
                parts = {}
                for match in re.finditer(r'(\w+)="(.*?)"', line):
                    parts[match.group(1)] = match.group(2)
                mountpoint = parts.get("MOUNTPOINT", "")
                if not mountpoint:
                    continue
                tran = parts.get("TRAN", "")
                name = parts.get("NAME", "")
                rm = parts.get("RM", "")
                if tran == "usb" or name.startswith("mmc") or rm == "1":
                    yield mountpoint
        except Exception:
            return

    def _iter_windows_drives(self):
        try:
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if mask & (1 << i):
                    yield f"{chr(65 + i)}:\\"
        except Exception:
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = f"{letter}:\\"
                if os.path.isdir(drive):
                    yield drive

    def _get_windows_drive_type(self, drive):
        try:
            import ctypes

            return ctypes.windll.kernel32.GetDriveTypeW(str(drive))
        except Exception:
            return None

    def find_radio_root_on_drives(self, removable_only=True):
        candidates = []

        if sys.platform == "win32":
            for drive in self._iter_windows_drives():
                try:
                    dtype = self._get_windows_drive_type(drive)
                    if removable_only and dtype not in (2, None):
                        continue
                    score = self._score_root(drive)
                    if score >= 0:
                        candidates.append((score, os.path.normpath(drive)))
                except Exception:
                    continue
        else:
            seen = set()
            for root in self._iter_mount_roots():
                if root in seen:
                    continue
                seen.add(root)
                score = self._score_root(root)
                if score >= 0:
                    candidates.append((score, os.path.normpath(root)))
            for root in self._iter_lsblk_mounts():
                if root in seen:
                    continue
                seen.add(root)
                score = self._score_root(root)
                if score >= 0:
                    candidates.append((score, os.path.normpath(root)))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1].lower()))
        return candidates[0][1]

    def get_radio_root(self):
        root = self.find_radio_root_on_drives(removable_only=True)
        if root:
            return root
        return self.find_radio_root_on_drives(removable_only=False)

    def get_scripts_dir(self):
        root = self.get_radio_root()
        if not root:
            return None
        return self._scripts_dir_in_root(root)


class UpdaterGUI:
    """Main GUI application."""

    def __init__(self, root):
        self.root = root
        self.root.title("Rotorflight Lua EdgeTX/OpenTX Updater")
        self.root.geometry("800x800")
        self.root.resizable(False, False)

        self.update_thread = None
        self.is_updating = False
        self.chkdsk_attempted = False
        self.settings_path = UPDATER_SETTINGS_FILE
        self.selected_version = tk.StringVar(value=VERSION_RELEASE)
        self.logo_image = None
        self.logo_label = None

        self._load_user_settings()
        self.setup_ui()
        self._bind_settings_autosave()

        self.radio = RadioStorageInterface(self.log)
        self.version_list = self.fetch_version_list()
        self._update_version_combo()

    def _load_user_settings(self):
        try:
            if not os.path.isfile(self.settings_path):
                return
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            version = str(data.get("version", "")).strip()
            if version in (VERSION_RELEASE, VERSION_SNAPSHOT, VERSION_MASTER):
                self.selected_version.set(version)
        except Exception:
            return

    def _save_user_settings(self, *_):
        try:
            _ensure_work_dir()
            data = {"version": self.selected_version.get()}
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            if hasattr(self, "log_text"):
                self.log(f"⚠ Could not save updater settings: {e}")

    def _bind_settings_autosave(self):
        self.selected_version.trace_add("write", self._save_user_settings)

    def setup_ui(self):
        header_bg = "#1f1f1f"
        header_fg = "#f2f2f2"
        title_frame = tk.Frame(self.root, bg=header_bg)
        title_frame.pack(fill=tk.X)
        title_frame.pack_propagate(False)
        title_frame.configure(height=90)

        text_frame = tk.Frame(title_frame, bg=header_bg)
        text_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=10)

        tk.Label(
            text_frame,
            text="Rotorflight Lua EdgeTX/OpenTX Updater",
            font=("Arial", 16, "bold"),
            bg=header_bg,
            fg=header_fg,
        ).pack(anchor=tk.W)

        tk.Label(
            text_frame,
            text="Update a mounted EdgeTX/OpenTX SD card from GitHub",
            font=("Arial", 10),
            bg=header_bg,
            fg=header_fg,
        ).pack(anchor=tk.W, pady=(2, 0))

        logo_frame = tk.Frame(title_frame, bg=header_bg, width=340, height=80)
        logo_frame.pack(side=tk.RIGHT, padx=(10, 10), pady=10)
        logo_frame.pack_propagate(False)
        self._load_logo(logo_frame, header_bg)

        version_frame = ttk.LabelFrame(self.root, text="Version Selection", padding=(14, 12))
        version_frame.pack(fill=tk.X, padx=10, pady=5)
        version_frame.columnconfigure(0, minsize=190)
        version_frame.columnconfigure(1, minsize=330)
        version_frame.columnconfigure(2, weight=1)

        ttk.Label(version_frame, text="Build channel:", font=("Arial", 9)).grid(
            row=0, column=0, padx=(0, 10), pady=(2, 8), sticky="E"
        )

        combo_index = {
            VERSION_RELEASE: 0,
            VERSION_SNAPSHOT: 1,
            VERSION_MASTER: 2,
        }
        self.version_filter_combo = ttk.Combobox(version_frame, width=34, state="readonly")
        self.version_filter_combo["values"] = ["Releases", "Snapshots", "Development"]
        self.version_filter_combo.current(combo_index.get(self.selected_version.get(), 0))
        self.version_filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_selected_version())
        self.version_filter_combo.grid(row=0, column=1, padx=(0, 14), pady=(2, 8), sticky="W")

        ttk.Label(version_frame, text="Select version to install:", font=("Arial", 9)).grid(
            row=1, column=0, padx=(0, 10), pady=(4, 2), sticky="E"
        )
        self.version_combo = ttk.Combobox(version_frame, width=34, state="readonly")
        self.version_combo["values"] = []
        self.version_combo.grid(row=1, column=1, padx=(0, 14), pady=(4, 2), sticky="W")

        ttk.Label(
            version_frame,
            text="SD card is detected automatically from the standard folder layout.",
            font=("Arial", 8),
            justify=tk.LEFT,
        ).grid(row=2, column=0, columnspan=3, padx=(0, 0), pady=(10, 0), sticky="W")

        status_frame = ttk.LabelFrame(self.root, text="Status", padding="10")
        status_frame.pack(fill=tk.X, padx=10, pady=5)

        self.status_label = ttk.Label(status_frame, text="Ready to update", font=("Arial", 10))
        self.status_label.pack()

        self.progress_label = ttk.Label(status_frame, text="", font=("Arial", 8))
        self.progress_label.pack()

        self.normal_step_names = ["Find", "Download", "Extract", "Copy", "Cleanup"]
        self.disk_step_names = ["Detect", "Prepare", "Scan", "Finalize"]
        self.step_names = list(self.normal_step_names)
        self.disk_progress_mode = False
        self.segment_bar = tk.Canvas(
            status_frame,
            height=36,
            highlightthickness=1,
            highlightbackground="#bdbdbd",
            bg="#f2f2f2",
        )
        self.segment_bar.pack(fill=tk.X, padx=8, pady=5)
        self.segment_states = [False for _ in self.step_names]
        self.segment_active_index = None
        self.segment_pulse_on = False
        self.segment_pulse_after_id = None
        self._draw_segment_bar()
        self.root.bind("<Configure>", lambda _e: self._draw_segment_bar())

        log_frame = ttk.LabelFrame(self.root, text="Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=15, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        button_frame = ttk.Frame(self.root, padding="10")
        button_frame.pack(fill=tk.X)

        self.update_button = ttk.Button(
            button_frame,
            text="Start Update",
            command=self.start_update,
            style="Accent.TButton",
        )
        self.update_button.pack(side=tk.LEFT, padx=5)

        self.cancel_button = ttk.Button(
            button_frame,
            text="Cancel",
            command=self.cancel_update,
            state=tk.DISABLED,
        )
        self.cancel_button.pack(side=tk.LEFT, padx=5)

        self.save_log_button = ttk.Button(button_frame, text="Save Log", command=self.save_log)
        self.save_log_button.pack(side=tk.LEFT, padx=5)

        self.clear_cache_button = ttk.Button(button_frame, text="Delete Cache", command=self.delete_cache)
        self.clear_cache_button.pack(side=tk.LEFT, padx=5)

        self.check_disk_button = ttk.Button(button_frame, text="Check Disk", command=self.check_disk)
        self.check_disk_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="Exit", command=self.root.quit).pack(side=tk.RIGHT, padx=5)
        self.exit_button = button_frame.winfo_children()[-1]

        info_frame = ttk.LabelFrame(self.root, text="Instructions", padding="10")
        info_frame.pack(fill=tk.X, padx=10, pady=5)

        info_text = (
            "1. Put the radio in USB storage mode, or insert the SD card into your computer\n"
            "2. Make sure the SD card contains SCRIPTS plus standard folders like MODELS/SOUNDS/TEMPLATES\n"
            "3. Choose the version you want to install\n"
            "4. Click 'Start Update' and wait for the update to complete"
        )
        ttk.Label(info_frame, text=info_text, font=("Arial", 8), justify=tk.LEFT).pack(anchor=tk.W)

        self.update_notice = ttk.Frame(self.root, padding="8")
        self.update_notice.pack(fill=tk.X, padx=10, pady=(0, 5))
        ttk.Button(
            self.update_notice,
            text="Download Latest Updater",
            command=lambda: webbrowser.open(UPDATER_INFO_URL),
        ).pack(side=tk.RIGHT)

    def _load_logo(self, logo_frame, header_bg):
        def set_logo_image(path):
            try:
                logo_img = tk.PhotoImage(file=str(path))
                target_h = logo_frame.winfo_reqheight() or 80
                target_w = logo_frame.winfo_reqwidth() or 340
                scale_h = math.ceil(logo_img.height() / target_h)
                scale_w = math.ceil(logo_img.width() / target_w)
                scale = max(1, scale_h, scale_w)
                if scale > 1:
                    logo_img = logo_img.subsample(scale, scale)
                self.logo_image = logo_img
                if not self.logo_label:
                    self.logo_label = tk.Label(logo_frame, image=self.logo_image, bg=header_bg)
                else:
                    self.logo_label.configure(image=self.logo_image)
                self.logo_label.place(relx=1.0, x=0, y=-5, anchor=tk.NE)
            except Exception:
                pass

        local_logo = RESOURCE_DIR / "logo.png"
        if not local_logo.is_file():
            local_logo = APP_DIR / "logo.png"
        if local_logo.is_file():
            set_logo_image(local_logo)

        def fetch_logo():
            try:
                _ensure_work_dir()
                req = Request(LOGO_URL, headers={"User-Agent": "Mozilla/5.0"})
                with self.urlopen_insecure(req, timeout=10) as response:
                    logo_bytes = response.read()
                tmp_logo = WORK_DIR / "rotorflight_edgetx_logo.png"
                with open(tmp_logo, "wb") as f:
                    f.write(logo_bytes)
                self.root.after(0, lambda: set_logo_image(tmp_logo))
            except Exception:
                pass

        self.root.after(100, lambda: threading.Thread(target=fetch_logo, daemon=True).start())

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def urlopen_insecure(self, req, timeout=10):
        context = ssl._create_unverified_context()
        return urlopen(req, timeout=timeout, context=context)

    def _download_cache_dir(self):
        path = WORK_DIR / CACHE_DIRNAME / "downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _download_cache_paths(self, url):
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        base = self._download_cache_dir() / key
        return str(base.with_suffix(".zip")), str(base.with_suffix(".json"))

    def _read_json_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_json_file(self, path, payload):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            pass

    def _download_zip_with_cache(self, download_url):
        cache_zip, cache_meta = self._download_cache_paths(download_url)
        meta = self._read_json_file(cache_meta)
        has_cache = os.path.isfile(cache_zip)

        if has_cache:
            self.log(f"  Cache candidate found: {os.path.basename(cache_zip)}")

        attempt = 0
        while True:
            attempt += 1
            headers = {"User-Agent": "Mozilla/5.0"}
            if has_cache:
                if meta.get("etag"):
                    headers["If-None-Match"] = meta["etag"]
                if meta.get("last_modified"):
                    headers["If-Modified-Since"] = meta["last_modified"]

            req = Request(download_url, headers=headers)
            try:
                self.log(f"  Download attempt {attempt}/{DOWNLOAD_RETRIES} (timeout {DOWNLOAD_TIMEOUT}s)")
                with self.urlopen_insecure(req, timeout=DOWNLOAD_TIMEOUT) as response:
                    total_size = int(response.headers.get("content-length", 0))
                    size_known = total_size > 0
                    downloaded = 0

                    if size_known:
                        self.update_progress(0, "Downloading...")
                    else:
                        total_size = 50 * 1024 * 1024
                        self.update_progress(0, "Downloading (size unknown)...")
                        self.log("  Download size unknown (no content-length); estimating 50MB")

                    last_log_percent = -1
                    tmp_zip = cache_zip + ".part"
                    with open(tmp_zip, "wb") as f:
                        while True:
                            if not self.is_updating:
                                return None
                            chunk = response.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            percent = (downloaded / total_size) * 100 if total_size > 0 else 0
                            if int(percent) != last_log_percent:
                                last_log_percent = int(percent)
                                if size_known:
                                    self.log(f"  Downloaded: {downloaded}/{total_size} bytes ({percent:.1f}%)")
                                else:
                                    self.log(f"  Downloaded: {downloaded}/{total_size} bytes ({percent:.1f}%) (estimated)")
                            self.update_progress(downloaded, f"Downloading... {percent:.1f}%")

                    os.replace(tmp_zip, cache_zip)
                    self._write_json_file(
                        cache_meta,
                        {
                            "url": download_url,
                            "etag": response.headers.get("ETag"),
                            "last_modified": response.headers.get("Last-Modified"),
                            "cached_at": int(time.time()),
                        },
                    )
                    self.log(f"✓ Downloaded {downloaded} bytes")
                    return cache_zip
            except HTTPError as e:
                if e.code == 304 and has_cache:
                    self.log("  Remote unchanged (HTTP 304). Using cached download.")
                    return cache_zip
                if attempt >= DOWNLOAD_RETRIES:
                    if has_cache:
                        self.log(f"⚠ Download failed ({e}); using cached download.")
                        return cache_zip
                    raise
                self.log(f"  Download failed: {e}. Retrying in {DOWNLOAD_RETRY_DELAY}s...")
                time.sleep(DOWNLOAD_RETRY_DELAY)
            except URLError as e:
                if attempt >= DOWNLOAD_RETRIES:
                    if has_cache:
                        self.log(f"⚠ Download failed ({e}); using cached download.")
                        return cache_zip
                    raise
                self.log(f"  Download failed: {e}. Retrying in {DOWNLOAD_RETRY_DELAY}s...")
                time.sleep(DOWNLOAD_RETRY_DELAY)

    def save_log(self):
        try:
            log_text = self.log_text.get("1.0", tk.END)
        except Exception:
            log_text = ""
        if not log_text.strip():
            messagebox.showinfo("Save Log", "There is no log content to save yet.")
            return
        filename = filedialog.asksaveasfilename(
            title="Save Updater Log",
            defaultextension=".txt",
            initialfile="rotorflight_edgetx_updater_log.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not filename:
            return
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(log_text)
            messagebox.showinfo("Save Log", f"Log saved to:\n{filename}")
        except Exception as e:
            messagebox.showerror("Save Log", f"Failed to save log:\n{e}")

    def delete_cache(self):
        if self.is_updating:
            messagebox.showinfo("Delete Cache", "Cannot delete cache while an update is running.")
            return
        confirmed = messagebox.askyesno(
            "Delete Cache",
            "Delete cached downloads and cached master sparse checkout?\n\n"
            "This will force fresh network fetches on the next update.",
        )
        if not confirmed:
            return
        ok, err = _clear_cache_dir()
        if ok:
            self.log("Cache deleted by user.")
            messagebox.showinfo("Delete Cache", "Updater cache deleted.")
        else:
            self.log(f"⚠ Failed to delete cache: {err}")
            messagebox.showerror("Delete Cache", f"Failed to delete cache:\n{err}")

    def _find_scripts_dir_for_maintenance(self):
        scripts_dir = self.radio.get_scripts_dir()
        if scripts_dir:
            return scripts_dir
        root = self.radio.find_radio_root_on_drives(removable_only=False)
        if not root:
            return None
        return self.radio._scripts_dir_in_root(root)

    def _check_disk_command_for_scripts(self, scripts_dir):
        if sys.platform == "win32":
            drive, _ = os.path.splitdrive(scripts_dir)
            if not drive:
                raise RuntimeError(f"Could not determine drive letter for: {scripts_dir}")
            return ["chkdsk", drive, "/f", "/x"], f"{drive}\\", None

        if sys.platform == "darwin":
            volume_root = os.path.abspath(os.path.join(scripts_dir, os.pardir))
            return ["diskutil", "repairVolume", volume_root], volume_root, None

        source = None
        mountpoint = None
        try:
            res = subprocess.run(
                ["findmnt", "-no", "SOURCE,TARGET", "--target", scripts_dir],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            line = (res.stdout or "").strip()
            if line:
                parts = line.split()
                if parts:
                    source = parts[0]
                if len(parts) > 1:
                    mountpoint = parts[1]
        except Exception:
            pass

        if not source:
            raise RuntimeError("Could not resolve mounted source device.")
        if not source.startswith("/dev/"):
            raise RuntimeError(f"Resolved source is not a block device: {source}")

        target_desc = f"{source} ({mountpoint or 'unknown mount'})"
        pre_cmd = ["umount", mountpoint] if mountpoint else None
        return ["fsck", "-y", source], target_desc, pre_cmd

    def _check_disk_worker(self, scripts_dir):
        def _run_disk_cmd(cmd, timeout=1800):
            self.log(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if result.stdout:
                for line in result.stdout.strip().splitlines():
                    if line.strip():
                        self.log(f"  [disk] {line}")
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    if line.strip():
                        self.log(f"  [disk] {line}")
            return result

        try:
            self.root.after(
                0,
                lambda: self._set_disk_phase(
                    current="Prepare",
                    status="Preparing disk check...",
                    text="Resolving disk-check command...",
                ),
            )
            cmd, target_desc, pre_cmd = self._check_disk_command_for_scripts(scripts_dir)
            self.log(f"Disk check target: {target_desc}")
            if pre_cmd:
                self.log("Unmounting volume before fsck...")
                pre = _run_disk_cmd(pre_cmd, timeout=120)
                if pre.returncode != 0:
                    raise RuntimeError(f"Unmount failed for {target_desc}. Please close any apps using the drive and retry.")
            self.root.after(
                0,
                lambda: self._set_disk_phase(
                    done="Prepare",
                    current="Scan",
                    status="Running disk check...",
                    text=f"Checking {target_desc}...",
                ),
            )
            result = _run_disk_cmd(cmd)
            self.root.after(
                0,
                lambda: self._set_disk_phase(
                    done="Scan",
                    current="Finalize",
                    status="Finalizing disk check...",
                    text="Collecting results...",
                ),
            )

            if result.returncode == 0:
                self.root.after(0, lambda: messagebox.showinfo("Check Disk", f"Disk check completed for {target_desc}."))
            else:
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Check Disk",
                        f"Disk check finished with code {result.returncode} for {target_desc}.\nSee log for details.",
                    ),
                )
        except FileNotFoundError as e:
            msg = f"Required tool is not available on this system: {e}"
            self.log(f"⚠ {msg}")
            self.root.after(0, lambda: messagebox.showerror("Check Disk", msg))
        except subprocess.TimeoutExpired:
            msg = "Disk check timed out."
            self.log(f"⚠ {msg}")
            self.root.after(0, lambda: messagebox.showerror("Check Disk", msg))
        except Exception as e:
            self.log(f"⚠ Disk check failed: {e}")
            self.root.after(0, lambda: self._set_disk_phase(current="Finalize", status="Disk check failed", text="Disk check failed."))
            self.root.after(0, lambda: messagebox.showerror("Check Disk", f"Disk check failed:\n{e}"))
        finally:
            def _finish_ui():
                self.mark_step_done("Finalize")
                self._set_disk_check_controls_running(False)
                self._exit_disk_check_progress_mode()

            self.root.after(0, _finish_ui)

    def check_disk(self):
        if self.is_updating:
            messagebox.showinfo("Check Disk", "Cannot run disk check while an update is running.")
            return

        self._enter_disk_check_progress_mode()
        self._set_disk_phase(current="Detect", status="Detecting SD card...", text="Detecting target volume...")
        scripts_dir = self._find_scripts_dir_for_maintenance()
        if not scripts_dir:
            self._exit_disk_check_progress_mode()
            messagebox.showerror("Check Disk", "Could not locate the SD-card SCRIPTS directory.")
            return
        self._set_disk_phase(done="Detect", current="Prepare", status="Preparing disk check...", text="Preparing check command...")

        if sys.platform == "win32":
            check_note = "This will run chkdsk /f /x on the SD-card drive (auto-fix, force dismount)."
        elif sys.platform == "darwin":
            check_note = "This will run diskutil repairVolume on the mounted SD-card volume."
        else:
            check_note = "This will run fsck -y on the detected source device (auto-fix)."

        confirmed = messagebox.askyesno(
            "Check Disk",
            f"{check_note}\n\nTarget scripts path:\n{scripts_dir}\n\nContinue?",
        )
        if not confirmed:
            self._exit_disk_check_progress_mode()
            return

        self.log("Starting disk check...")
        self._set_disk_check_controls_running(True)
        threading.Thread(target=self._check_disk_worker, args=(scripts_dir,), daemon=True).start()

    def set_status(self, message):
        self.status_label.config(text=message)
        self.root.update_idletasks()

    def update_progress(self, value, text=""):
        self.progress_label.config(text=text)
        self.root.update_idletasks()

    def _update_selected_version(self):
        selected = self.version_filter_combo.current()
        if selected == 0:
            self.selected_version.set(VERSION_RELEASE)
        elif selected == 1:
            self.selected_version.set(VERSION_SNAPSHOT)
        else:
            self.selected_version.set(VERSION_MASTER)
        self._update_version_combo()

    def _update_version_combo(self):
        combo_values = []
        version_type = self.selected_version.get()

        for display_name, version_data in self.version_list.items():
            if version_data.get("version_type") == version_type:
                combo_values.append(display_name)

        if version_type == VERSION_MASTER:
            master_entries = [value for value in combo_values if value == "Master"]
            other_entries = [value for value in combo_values if value != "Master"]
            combo_values = master_entries + other_entries

        if not combo_values:
            combo_values = ["No valid versions found"]

        self.version_combo["values"] = combo_values
        self.version_combo.current(0)

    def _draw_segment_bar(self):
        if not hasattr(self, "segment_bar"):
            return
        self.segment_bar.delete("all")
        width = max(1, self.segment_bar.winfo_width())
        height = int(self.segment_bar["height"])
        padding = 6
        gap = 4
        label_h = 14
        bar_h = height - padding * 2 - label_h
        bar_y1 = padding
        bar_y2 = padding + bar_h
        total_segments = len(self.step_names)
        seg_w = max(1, (width - padding * 2 - gap * (total_segments - 1)) // total_segments)
        x = padding
        for i, name in enumerate(self.step_names):
            if self.segment_states[i]:
                fill = "#1db954"
            elif self.segment_active_index == i:
                fill = "#f4a259" if self.segment_pulse_on else "#d9d9d9"
            else:
                fill = "#d9d9d9"
            self.segment_bar.create_rectangle(x, bar_y1, x + seg_w, bar_y2, fill=fill, outline="#bdbdbd")
            self.segment_bar.create_text(
                x + seg_w / 2,
                bar_y2 + label_h / 2,
                text=name,
                fill="#333333",
                font=("Arial", 8),
            )
            x += seg_w + gap

    def reset_steps(self):
        self.segment_states = [False for _ in self.step_names]
        self._stop_segment_pulse()
        self._draw_segment_bar()

    def mark_step_done(self, step_name):
        if step_name not in self.step_names:
            return
        idx = self.step_names.index(step_name)
        self.segment_states[idx] = True
        if self.segment_active_index == idx:
            self._stop_segment_pulse()
        self._draw_segment_bar()

    def set_current_step(self, step_name):
        if step_name in self.step_names:
            self.segment_active_index = self.step_names.index(step_name)
            self._start_segment_pulse()
        self.update_progress(0, f"Current step: {step_name}")

    def _enter_disk_check_progress_mode(self):
        if self.disk_progress_mode:
            return
        self.disk_progress_mode = True
        self.step_names = list(self.disk_step_names)
        self.reset_steps()

    def _exit_disk_check_progress_mode(self):
        if not self.disk_progress_mode:
            return
        self.disk_progress_mode = False
        self.step_names = list(self.normal_step_names)
        self.reset_steps()
        self.update_progress(0, "")
        self.set_status("Ready to update")

    def _set_disk_phase(self, current=None, done=None, status=None, text=None):
        if status is not None:
            self.set_status(status)
        if done is not None:
            self.mark_step_done(done)
        if current is not None:
            self.set_current_step(current)
        if text is not None:
            self.update_progress(0, text)

    def _set_disk_check_controls_running(self, running):
        if running:
            self.update_button.config(state=tk.DISABLED)
            self.clear_cache_button.config(state=tk.DISABLED)
            self.check_disk_button.config(state=tk.DISABLED)
            self.exit_button.config(state=tk.DISABLED)
        else:
            self.update_button.config(state=tk.NORMAL)
            self.clear_cache_button.config(state=tk.NORMAL)
            self.check_disk_button.config(state=tk.NORMAL)
            self.exit_button.config(state=tk.NORMAL)

    def _set_update_controls_running(self, running):
        if running:
            self.update_button.config(state=tk.DISABLED)
            self.cancel_button.config(state=tk.NORMAL)
            self.clear_cache_button.config(state=tk.DISABLED)
            self.check_disk_button.config(state=tk.DISABLED)
            self.exit_button.config(state=tk.DISABLED)
        else:
            self.update_button.config(state=tk.NORMAL)
            self.cancel_button.config(state=tk.DISABLED)
            self.clear_cache_button.config(state=tk.NORMAL)
            self.check_disk_button.config(state=tk.NORMAL)
            self.exit_button.config(state=tk.NORMAL)

    def _start_segment_pulse(self):
        if self.segment_pulse_after_id is not None:
            return
        self.segment_pulse_on = False
        self._pulse_active_segment()

    def _stop_segment_pulse(self):
        if self.segment_pulse_after_id is not None:
            try:
                self.root.after_cancel(self.segment_pulse_after_id)
            except Exception:
                pass
        self.segment_pulse_after_id = None
        self.segment_pulse_on = False
        self.segment_active_index = None

    def _pulse_active_segment(self):
        if self.segment_active_index is None:
            self.segment_pulse_after_id = None
            return
        self.segment_pulse_on = not self.segment_pulse_on
        self._draw_segment_bar()
        self.segment_pulse_after_id = self.root.after(500, self._pulse_active_segment)

    def count_files(self, directory):
        total = 0
        for _root, _dirs, files in os.walk(directory):
            total += len(files)
        return total

    def _file_md5(self, path, chunk=1024 * 1024):
        h = hashlib.md5()
        with open(path, "rb", buffering=0) as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                h.update(data)
        return h.hexdigest()

    def _needs_copy_with_md5(self, srcf, dstf, ts_slack=TS_SLACK_SECONDS):
        try:
            ss = os.stat(srcf)
        except FileNotFoundError:
            return False
        if not os.path.exists(dstf):
            return True
        try:
            ds = os.stat(dstf)
        except FileNotFoundError:
            return True
        if ss.st_size != ds.st_size:
            return True
        if abs(ss.st_mtime - ds.st_mtime) <= ts_slack:
            return False
        try:
            return self._file_md5(srcf) != self._file_md5(dstf)
        except Exception:
            return True

    def _is_ignored_path(self, path, root_dir):
        rel = os.path.relpath(path, root_dir)
        rel_norm = rel.replace("\\", "/")
        parts = [p for p in rel_norm.split("/") if p and p != "."]
        for part in parts:
            if part in ("__pycache__", "._pycache__"):
                return True
            if part.startswith("._"):
                return True
        base = os.path.basename(path)
        if base.endswith((".pyc", ".pyo")):
            return True
        return False

    def _build_rel_file_map(self, root_dir):
        files = {}
        if not os.path.isdir(root_dir):
            return files
        for root, dirs, names in os.walk(root_dir):
            dirs[:] = [d for d in dirs if not self._is_ignored_path(os.path.join(root, d), root_dir)]
            for name in names:
                full = os.path.join(root, name)
                if self._is_ignored_path(full, root_dir):
                    continue
                rel = os.path.relpath(full, root_dir)
                files[rel] = full
        return files

    def _remove_empty_dirs(self, root_dir):
        if not os.path.isdir(root_dir):
            return
        for root, dirs, files in os.walk(root_dir, topdown=False):
            if dirs or files:
                continue
            try:
                os.rmdir(root)
            except Exception:
                pass

    def remove_stale_files_with_progress(self, src, dst):
        if not os.path.isdir(dst):
            return True

        src_files = self._build_rel_file_map(src)
        dst_files = self._build_rel_file_map(dst)
        stale = [rel for rel in dst_files.keys() if rel not in src_files]
        total_stale = len(stale)
        self.log(f"  Total stale files to delete: {total_stale}")

        removed = 0
        for rel in stale:
            if not self.is_updating:
                return False
            file_path = dst_files.get(rel) or os.path.join(dst, rel)
            try:
                attempt = 0
                while True:
                    try:
                        os.remove(file_path)
                        break
                    except OSError as e:
                        attempt += 1
                        winerr = getattr(e, "winerror", None)
                        if winerr == 483 and attempt < 3:
                            time.sleep(0.5)
                            continue
                        raise
                removed += 1
                time.sleep(COPY_SETTLE_SECONDS)

                percent = (removed / total_stale) * 100 if total_stale else 100
                self.update_progress(removed, f"Removed stale {removed}/{total_stale} files ({percent:.1f}%)")
                if removed % 10 == 0 or removed == total_stale:
                    self.log(f"  [DEL {removed}/{total_stale}] {rel}")
            except Exception as e:
                winerr = getattr(e, "winerror", None)
                if winerr == 483:
                    self.log(f"  ⚠ Device error while deleting stale file {os.path.basename(file_path)}.")
                    self.attempt_chkdsk(file_path)
                    return False
                self.log(f"  ⚠ Failed to delete stale file {rel}: {e}")

        self._remove_empty_dirs(dst)
        return True

    def attempt_chkdsk(self, path):
        if self.chkdsk_attempted:
            return False

        self.chkdsk_attempted = True
        if sys.platform == "win32":
            drive, _ = os.path.splitdrive(path)
            if not drive:
                return False
            self.log(f"Detected filesystem error. Running chkdsk {drive} /f ...")
            try:
                result = subprocess.run(
                    ["chkdsk", drive, "/f"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    creationflags=CREATE_NO_WINDOW,
                )
                if result.stdout:
                    for line in result.stdout.strip().splitlines()[:8]:
                        self.log(f"  [chkdsk] {line}")
                if result.stderr:
                    for line in result.stderr.strip().splitlines()[:8]:
                        self.log(f"  [chkdsk] {line}")
            except Exception as e:
                self.log(f"  [chkdsk] Failed to run: {e}")

            try:
                messagebox.showinfo(
                    "Filesystem Repair",
                    f"CHKDSK was run on {drive}. Please click Update again to retry.",
                )
            except Exception:
                pass
            return True

        return False

    def copy_tree_with_progress(self, src, dst):
        os.makedirs(dst, exist_ok=True)
        src_files = self._build_rel_file_map(src)
        total_files = len(src_files)
        self.log(f"  Total files to verify: {total_files}")

        to_copy = []
        checked = 0
        for rel, src_file in src_files.items():
            if not self.is_updating:
                return False
            dst_file = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            if self._needs_copy_with_md5(src_file, dst_file):
                to_copy.append((rel, src_file, dst_file))
            checked += 1
            if checked % 50 == 0 or checked == total_files:
                percent = (checked / total_files) * 100 if total_files else 100
                self.update_progress(checked, f"Verified {checked}/{total_files} files ({percent:.1f}%)")

        self.log(f"  Changed/new files to copy: {len(to_copy)}")
        copied = 0
        for rel, src_file, dst_file in to_copy:
            if not self.is_updating:
                return False
            try:
                shutil.copy2(src_file, dst_file)
                copied += 1
                time.sleep(COPY_SETTLE_SECONDS)
                percent = (copied / len(to_copy)) * 100 if to_copy else 100
                self.update_progress(copied, f"Copied {copied}/{len(to_copy)} files ({percent:.1f}%)")
                if copied % 10 == 0 or copied == len(to_copy):
                    self.log(f"  [COPY {copied}/{len(to_copy)}] {rel}")
            except Exception as e:
                winerr = getattr(e, "winerror", None)
                if winerr == 483:
                    self.log(f"  ⚠ Device error while copying {os.path.basename(src_file)}.")
                    self.attempt_chkdsk(dst_file)
                    return False
                self.log(f"  ⚠ Failed to copy {os.path.basename(src_file)}: {e}")

        if not to_copy:
            self.log("  No changed files detected.")
        return True

    def _get_url_by_name(self, asset_name, assets):
        for asset in assets:
            if asset.get("name") == asset_name:
                return asset.get("browser_download_url")
        return None

    def fetch_version_list(self):
        version_list = {
            "Master": {
                "display_name": "Master",
                "tag_name": "master",
                "download_url": f"{GITHUB_REPO_URL}/archive/refs/heads/master.zip",
                "is_asset": False,
                "version_type": VERSION_MASTER,
            }
        }
        release_assets_by_tag = {}
        seen_tags = set()

        def fetch_json(url):
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with self.urlopen_insecure(req, timeout=DOWNLOAD_TIMEOUT) as response:
                return json.loads(response.read().decode())

        def add_tag(tag_name, assets=None):
            if not tag_name or tag_name in seen_tags:
                return
            if tag_name.startswith("release/"):
                version_type = VERSION_RELEASE
                display_prefix = "Release"
                asset_prefix = "rotorflight-lua-scripts"
            elif tag_name.startswith("snapshot/"):
                version_type = VERSION_SNAPSHOT
                display_prefix = "Snapshot"
                asset_prefix = "rotorflight-lua-scripts-snapshot"
            else:
                return

            seen_tags.add(tag_name)
            version = tag_name.split("/", 1)[1]
            display_name = f"{display_prefix} {version}"
            assets = assets or release_assets_by_tag.get(tag_name, [])
            asset_name = f"{asset_prefix}-{version}.zip"
            asset_url = self._get_url_by_name(asset_name, assets)
            is_asset = bool(asset_url)
            if asset_url:
                self.log(f"✓ Found asset: {asset_name}")
            else:
                asset_url = f"{GITHUB_REPO_URL}/archive/refs/tags/{tag_name}.zip"
                self.log(f"✓ Found tag: {tag_name} (source ZIP fallback)")

            version_list[display_name] = {
                "display_name": display_name,
                "tag_name": tag_name,
                "download_url": asset_url,
                "is_asset": is_asset,
                "version_type": version_type,
            }

        def add_development_commits():
            try:
                commits = fetch_json(f"{GITHUB_API_URL}/commits?sha=master&per_page=20")
                if not isinstance(commits, list):
                    return
                for commit in commits:
                    sha = commit.get("sha", "")
                    if not sha:
                        continue
                    sha7 = sha[:7]
                    message = (commit.get("commit", {}).get("message") or "").splitlines()[0].strip()
                    if len(message) > 48:
                        message = message[:45] + "..."
                    display_name = sha7
                    if message:
                        display_name += f" - {message}"
                    version_list[display_name] = {
                        "display_name": display_name,
                        "tag_name": f"commit-{sha7}",
                        "download_url": f"{GITHUB_REPO_URL}/archive/{sha}.zip",
                        "is_asset": False,
                        "version_type": VERSION_MASTER,
                    }
            except Exception as e:
                self.log(f"⚠ Failed to fetch recent development commits: {e}")

        try:
            self.log("Fetching release and snapshot tags...")
            releases = fetch_json(f"{GITHUB_API_URL}/releases?per_page=100")
            for release in releases:
                tag_name = release.get("tag_name", "")
                if tag_name:
                    release_assets_by_tag[tag_name] = release.get("assets", [])
                    add_tag(tag_name, release_assets_by_tag[tag_name])

            tags = fetch_json(f"{GITHUB_API_URL}/tags?per_page=100")
            for tag in tags:
                add_tag(tag.get("name", ""))

            add_development_commits()
        except Exception as e:
            self.log(f"⚠ Failed to fetch version list: {e}")
            self.log("  Falling back to master branch")

        return version_list

    def get_download_url_and_name(self):
        selected = self.version_list.get(self.version_combo.get())
        if selected is not None:
            return selected["download_url"], selected["tag_name"], selected["is_asset"]
        fallback = self.version_list["Master"]
        return fallback["download_url"], fallback["tag_name"], fallback["is_asset"]

    def is_git_available(self):
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return result.returncode == 0
        except Exception:
            return False

    def sparse_checkout_master(self, dest_dir):
        if not self.is_git_available():
            self.log("⚠ Git not available; falling back to ZIP download")
            return False

        cache_repo = WORK_DIR / CACHE_DIRNAME / "master_sparse_repo"
        os.makedirs(cache_repo, exist_ok=True)
        self.log(f"Using git sparse cache for master: {cache_repo}")

        def run_git(args, cwd, timeout=60, progress_cb=None):
            cmd = ["git"] + args
            self.log(f"  Git: {' '.join(cmd)}")
            if "fetch" in args and progress_cb:
                output_lines = []
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=cwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True,
                        creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    percent_re = re.compile(r"(\d+)%")
                    last_percent = -1
                    for line in iter(proc.stdout.readline, ""):
                        if not line:
                            break
                        output_lines.append(line)
                        line_stripped = line.strip()
                        if line_stripped:
                            self.log(f"    [git] {line_stripped}")
                        match = percent_re.search(line_stripped)
                        if match:
                            pct = int(match.group(1))
                            if pct > last_percent:
                                last_percent = pct
                                progress_cb(pct)
                    proc.wait(timeout=timeout)
                    return subprocess.CompletedProcess(cmd, proc.returncode, stdout="".join(output_lines), stderr="")
                except subprocess.TimeoutExpired as e:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return subprocess.CompletedProcess(cmd, 1, stdout="".join(output_lines), stderr=str(e))
            else:
                result = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                if result.stdout:
                    for line in result.stdout.strip().splitlines():
                        if line.strip():
                            self.log(f"    [git] {line}")
                if result.stderr:
                    for line in result.stderr.strip().splitlines():
                        if line.strip():
                            self.log(f"    [git] {line}")
                return result

        git_dir = os.path.join(cache_repo, ".git")
        if not os.path.isdir(git_dir):
            init = run_git(["init"], cwd=str(cache_repo))
            if init.returncode != 0:
                self.log(f"⚠ Git init failed: {init.stderr.strip()}")
                return False
            add_origin = run_git(["remote", "add", "origin", GITHUB_REPO_URL + ".git"], cwd=str(cache_repo))
            if add_origin.returncode != 0:
                self.log(f"⚠ Git remote add failed: {add_origin.stderr.strip()}")
                return False

        run_git(["config", "core.sparseCheckout", "true"], cwd=str(cache_repo))
        run_git(["config", "advice.detachedHead", "false"], cwd=str(cache_repo))

        sparse_file = os.path.join(cache_repo, ".git", "info", "sparse-checkout")
        os.makedirs(os.path.dirname(sparse_file), exist_ok=True)
        with open(sparse_file, "w", encoding="utf-8") as f:
            f.write("src/SCRIPTS/\n")
            f.write("src/WIDGETS/\n")

        fetch = run_git(
            ["fetch", "--depth", "1", "--progress", "origin", "master"],
            cwd=str(cache_repo),
            timeout=180,
            progress_cb=lambda pct: self.update_progress(pct, f"Fetching master... {pct}%"),
        )
        cache_ready = False
        if fetch.returncode != 0:
            self.log(f"⚠ Git fetch failed: {fetch.stderr.strip()}")
            if os.path.isdir(os.path.join(cache_repo, "src", "SCRIPTS")):
                self.log("⚠ Using previously cached master snapshot.")
                cache_ready = True
            else:
                return False
        else:
            checkout = run_git(["checkout", "-f", "FETCH_HEAD"], cwd=str(cache_repo))
            if checkout.returncode != 0:
                self.log(f"⚠ Git checkout failed: {checkout.stderr.strip()}")
                if not os.path.isdir(os.path.join(cache_repo, "src", "SCRIPTS")):
                    return False
            else:
                cache_ready = True

        if not cache_ready and not os.path.isdir(os.path.join(cache_repo, "src", "SCRIPTS")):
            self.log("⚠ Sparse cache does not contain required source tree.")
            return False

        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)
        os.makedirs(dest_dir, exist_ok=True)

        staged_paths = ["src/SCRIPTS", "src/WIDGETS"]
        for rel in staged_paths:
            src_path = os.path.join(cache_repo, rel)
            dst_path = os.path.join(dest_dir, rel)
            if not os.path.exists(src_path):
                if rel == "src/SCRIPTS":
                    self.log(f"⚠ Missing required path in cache: {rel}")
                    return False
                self.log(f"⚠ Optional path missing in cache: {rel}")
                continue
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            if os.path.isdir(src_path):
                def _ignore_ephemeral(_dir, names):
                    ignored = []
                    for name in names:
                        if name in ("__pycache__", "._pycache__"):
                            ignored.append(name)
                            continue
                        if name.startswith("._"):
                            ignored.append(name)
                            continue
                        if name.endswith((".pyc", ".pyo")):
                            ignored.append(name)
                    return ignored

                shutil.copytree(src_path, dst_path, dirs_exist_ok=True, ignore=_ignore_ephemeral)
            else:
                shutil.copy2(src_path, dst_path)

        self.log("✓ Sparse checkout cache updated and staged")
        return True

    def _extract_repo_root(self, extract_dir):
        items = [item for item in os.listdir(extract_dir) if item not in ("__MACOSX",)]
        if len(items) == 1:
            only_name = items[0]
            only_path = os.path.join(extract_dir, only_name)
            # Snapshot/release assets may contain SCRIPTS or WIDGETS directly at
            # the archive root. Keep extract_dir as the install root in that case.
            if os.path.isdir(only_path) and only_name.upper() not in INSTALL_DIR_NAMES:
                return only_path
        return extract_dir

    def locate_install_root(self, repo_dir):
        if not repo_dir or not os.path.isdir(repo_dir):
            return None

        candidates = []
        seen = set()

        def add_candidate(path):
            if not path or not os.path.isdir(path):
                return
            norm = os.path.normpath(path)
            if norm in seen:
                return
            seen.add(norm)
            candidates.append(norm)

        add_candidate(repo_dir)
        add_candidate(os.path.join(repo_dir, "src"))

        base_name = os.path.basename(os.path.normpath(repo_dir)).upper()
        if base_name in INSTALL_DIR_NAMES:
            add_candidate(os.path.dirname(repo_dir))

        # Some archives wrap the install tree in an extra top-level folder such
        # as "package" or "artifacts". Search one level below the extracted
        # root before giving up.
        try:
            for name in sorted(os.listdir(repo_dir)):
                add_candidate(os.path.join(repo_dir, name))
        except OSError:
            pass

        for candidate in candidates:
            if any(os.path.isdir(os.path.join(candidate, name)) for name in INSTALL_DIR_NAMES):
                return candidate
        return None

    def build_install_specs(self, install_root):
        """
        Build a conservative install plan.

        We fully own dedicated namespaces like SCRIPTS/RF2 and widget folders,
        but only copy individual files into shared namespaces like
        SCRIPTS/TOOLS and SCRIPTS/FUNCTIONS.
        """
        specs = []

        scripts_root = os.path.join(install_root, "SCRIPTS")
        if os.path.isdir(scripts_root):
            for name in sorted(os.listdir(scripts_root)):
                src_path = os.path.join(scripts_root, name)
                rel_path = os.path.join("SCRIPTS", name)

                if os.path.isfile(src_path):
                    specs.append({"kind": "file", "src": src_path, "dst_rel": rel_path, "owned": False})
                    continue

                if not os.path.isdir(src_path):
                    continue

                if name in ("FUNCTIONS", "TOOLS"):
                    for child_rel, child_src in sorted(self._build_rel_file_map(src_path).items()):
                        specs.append(
                            {
                                "kind": "file",
                                "src": child_src,
                                "dst_rel": os.path.join(rel_path, child_rel),
                                "owned": False,
                            }
                        )
                else:
                    specs.append({"kind": "dir", "src": src_path, "dst_rel": rel_path, "owned": True})

        widgets_root = os.path.join(install_root, "WIDGETS")
        if os.path.isdir(widgets_root):
            for name in sorted(os.listdir(widgets_root)):
                src_path = os.path.join(widgets_root, name)
                rel_path = os.path.join("WIDGETS", name)
                if os.path.isdir(src_path):
                    specs.append({"kind": "dir", "src": src_path, "dst_rel": rel_path, "owned": True})
                elif os.path.isfile(src_path):
                    specs.append({"kind": "file", "src": src_path, "dst_rel": rel_path, "owned": True})

        return specs

    def copy_file_specs_with_progress(self, file_specs, radio_root):
        if not file_specs:
            return True

        total_files = len(file_specs)
        self.log(f"  Shared-namespace files to verify: {total_files}")

        to_copy = []
        checked = 0
        for spec in file_specs:
            if not self.is_updating:
                return False
            src_file = spec["src"]
            dst_file = os.path.join(radio_root, spec["dst_rel"])
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            if self._needs_copy_with_md5(src_file, dst_file):
                to_copy.append((spec["dst_rel"], src_file, dst_file))
            checked += 1
            if checked % 25 == 0 or checked == total_files:
                percent = (checked / total_files) * 100 if total_files else 100
                self.update_progress(checked, f"Verified shared files {checked}/{total_files} ({percent:.1f}%)")

        self.log(f"  Shared-namespace files to copy: {len(to_copy)}")
        copied = 0
        for dst_rel, src_file, dst_file in to_copy:
            if not self.is_updating:
                return False
            try:
                shutil.copy2(src_file, dst_file)
                copied += 1
                time.sleep(COPY_SETTLE_SECONDS)
                percent = (copied / len(to_copy)) * 100 if to_copy else 100
                self.update_progress(copied, f"Copied shared files {copied}/{len(to_copy)} ({percent:.1f}%)")
                self.log(f"  [COPY FILE {copied}/{len(to_copy)}] {dst_rel}")
            except Exception as e:
                winerr = getattr(e, "winerror", None)
                if winerr == 483:
                    self.log(f"  ⚠ Device error while copying {os.path.basename(src_file)}.")
                    self.attempt_chkdsk(dst_file)
                    return False
                self.log(f"  ⚠ Failed to copy {os.path.basename(src_file)}: {e}")

        if not to_copy:
            self.log("  No changed shared-namespace files detected.")
        return True

    def get_master_commit_suffix(self):
        try:
            req = Request(f"{GITHUB_API_URL}/commits/master", headers={"User-Agent": "Mozilla/5.0"})
            with self.urlopen_insecure(req, timeout=DOWNLOAD_TIMEOUT) as response:
                data = json.loads(response.read().decode())
                sha = data.get("sha", "")
                if sha:
                    return sha[:7]
        except Exception as e:
            self.log(f"⚠ Failed to fetch master commit SHA: {e}")
        return "master"

    def derive_version_label(self, version_type, version_name):
        if version_name.startswith("release/"):
            return version_name.split("/", 1)[1]
        if version_name.startswith("snapshot/"):
            return version_name.split("/", 1)[1]
        if version_type == VERSION_MASTER:
            if version_name.startswith("commit-"):
                return version_name
            return f"master-{self.get_master_commit_suffix()}"
        return version_name or "master"

    def read_rf2_lua_version(self, rf2_lua_path):
        try:
            with open(rf2_lua_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self.log(f"⚠ Unable to read rf2.lua for version info: {e}")
            return None

        match = re.search(r'luaVersion\s*=\s*"([^"]+)"', content)
        if not match:
            self.log("⚠ Could not parse luaVersion from rf2.lua")
            return None
        return match.group(1)

    def update_rf2_lua_version(self, rf2_lua_path, version_label):
        try:
            with open(rf2_lua_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self.log(f"⚠ Unable to read rf2.lua for version update: {e}")
            return False

        match = re.search(r'(luaVersion\s*=\s*")([^"]+)(")', content)
        if not match:
            self.log("⚠ luaVersion pattern not found in rf2.lua")
            return False

        current = match.group(2)
        base = re.sub(r"-master-[0-9a-f]{7}$", "", current)
        if re.match(r".+-commit-[0-9a-f]{7}$", current):
            base = re.sub(r"-commit-[0-9a-f]{7}$", "", current)
        if current.endswith(version_label):
            new_value = current
        else:
            new_value = f"{base}-{version_label}"

        updated = re.sub(r'(luaVersion\s*=\s*")([^"]+)(")', rf"\g<1>{new_value}\g<3>", content, count=1)
        try:
            with open(rf2_lua_path, "w", encoding="utf-8") as f:
                f.write(updated)
            self.log(f"✓ Updated rf2.lua version to '{new_value}'")
            return True
        except Exception as e:
            self.log(f"⚠ Unable to write rf2.lua version update: {e}")
            return False

    def start_update(self):
        if self.is_updating:
            return

        _ensure_work_dir()
        self.is_updating = True
        self.chkdsk_attempted = False
        self._set_update_controls_running(True)
        self.reset_steps()
        self.update_progress(0, "Starting...")
        self.update_thread = threading.Thread(target=self.update_process, daemon=True)
        self.update_thread.start()

    def cancel_update(self):
        self.is_updating = False
        self.log("Update cancelled by user")
        self.set_status("Update cancelled")
        self._stop_segment_pulse()
        self.update_progress(0, "")
        self._set_update_controls_running(False)

    def update_process(self):
        temp_dir = None
        try:
            self.update_progress(0, "Starting...")

            self.set_status("Looking for SD card...")
            self.set_current_step("Find")
            self.log("Detecting mounted EdgeTX/OpenTX SD card...")

            radio_root = self.radio.get_radio_root()
            if not radio_root:
                raise RuntimeError(
                    "Could not find a mounted EdgeTX/OpenTX SD card.\n\n"
                    "Expected layout: SCRIPTS plus at least three standard folders such as "
                    "MODELS, SOUNDS, TEMPLATES, RADIO, THEMES, LOGS, or WIDGETS."
                )

            scripts_dir = self.radio._scripts_dir_in_root(radio_root)
            if not scripts_dir:
                raise RuntimeError(f"No SCRIPTS directory found at {radio_root}")

            self.log(f"✓ Found SD-card root: {radio_root}")
            self.log(f"  Found scripts directory: {scripts_dir}")
            self.mark_step_done("Find")

            if not self.is_updating:
                return

            self.set_status("Preparing download...")
            self.set_current_step("Download")

            version_type = self.selected_version.get()
            download_url, version_name, is_asset = self.get_download_url_and_name()
            if not download_url:
                raise RuntimeError("No download URL available")
            version_label = self.derive_version_label(version_type, version_name)
            self.log(f"Selected version: {version_name or version_type}")
            self.log(f"Version label: {version_label}")

            _ensure_work_dir()
            temp_dir = tempfile.mkdtemp(prefix="rotorflight-edgetx-update-", dir=str(WORK_DIR))
            zip_path = None

            repo_dir = None
            if version_type == VERSION_MASTER and version_name == "master":
                self.set_status("Fetching master via git...")
                self.update_progress(0, "Fetching master via git...")
                self.log("Git sparse checkout: src/SCRIPTS/, src/WIDGETS/")
                repo_dir = os.path.join(temp_dir, "repo")
                if not self.sparse_checkout_master(repo_dir):
                    repo_dir = None
                else:
                    self.log("✓ Using sparse checkout; skipping ZIP download")
                    self.mark_step_done("Download")

            if repo_dir is None:
                self.log(f"Downloading from: {download_url}")
                try:
                    zip_path = self._download_zip_with_cache(download_url)
                    if not zip_path:
                        return
                    self.mark_step_done("Download")
                except (URLError, HTTPError) as e:
                    self.log(f"✗ Download failed: {e}")
                    raise

            if not self.is_updating:
                return

            extract_dir = None
            if repo_dir is None:
                self.set_status("Extracting archive...")
                self.set_current_step("Extract")
                self.log("Extracting downloaded archive...")

                extract_dir = os.path.join(temp_dir, "extracted")
                try:
                    with zipfile.ZipFile(zip_path, "r") as zip_ref:
                        skipped = 0
                        for member in zip_ref.infolist():
                            name = member.filename.replace("\\", "/")
                            parts = [p for p in name.split("/") if p and p != "."]
                            ignore = False
                            for part in parts:
                                if part in ("__pycache__", "._pycache__") or part.startswith("._"):
                                    ignore = True
                                    break
                            if not ignore and name.endswith((".pyc", ".pyo")):
                                ignore = True
                            if ignore:
                                skipped += 1
                                continue
                            zip_ref.extract(member, extract_dir)
                        if skipped:
                            self.log(f"  Skipped {skipped} ephemeral archive entries")
                    self.log("✓ Archive extracted")
                    self.mark_step_done("Extract")
                except Exception as e:
                    self.log(f"✗ Extraction failed: {e}")
                    raise
            else:
                self.mark_step_done("Extract")

            if not self.is_updating:
                return

            self.log("Locating source files...")
            if repo_dir is None:
                repo_dir = self._extract_repo_root(extract_dir)

            install_root = self.locate_install_root(repo_dir)
            if install_root:
                rel_install_root = os.path.relpath(install_root, repo_dir)
                self.log(f"✓ Found install root: {rel_install_root if rel_install_root != '.' else 'archive root'}")
            else:
                raise RuntimeError("Could not find installable SCRIPTS/WIDGETS content in extracted archive")

            if not is_asset:
                self.log("⚠ Using source-tree content. Development installs may be less optimized than release assets.")

            if not self.is_updating:
                return

            self.set_status("Syncing files to SD card...")
            self.set_current_step("Copy")

            install_specs = self.build_install_specs(install_root)
            owned_specs = [spec for spec in install_specs if spec["kind"] == "dir"]
            file_specs = [spec for spec in install_specs if spec["kind"] == "file"]

            installed_any = []
            for spec in owned_specs:
                src_dir = spec["src"]
                dst_dir = os.path.join(radio_root, spec["dst_rel"])
                installed_any.append(spec["dst_rel"])
                self.log(f"Syncing owned path {spec['dst_rel']}...")
                self.set_status(f"Removing stale files from {spec['dst_rel']}...")
                if not self.remove_stale_files_with_progress(src_dir, dst_dir):
                    self.log("⚠ Stale cleanup cancelled")
                    return
                if not self.is_updating:
                    return
                self.log(f"  Copying changed files for {spec['dst_rel']}...")
                self.set_status(f"Copying files to {spec['dst_rel']}...")
                if not self.copy_tree_with_progress(src_dir, dst_dir):
                    self.log("⚠ Copy cancelled")
                    return

            if file_specs:
                installed_any.extend(spec["dst_rel"] for spec in file_specs)
                self.log("Syncing shared-namespace files without deleting unrelated content...")
                self.set_status("Copying shared-namespace files...")
                if not self.copy_file_specs_with_progress(file_specs, radio_root):
                    self.log("⚠ Shared-file copy cancelled")
                    return

            if not installed_any:
                raise RuntimeError("No install paths were found to sync.")

            self.mark_step_done("Copy")
            self.log("✓ Files synced to SD card successfully")

            rf2_lua_path = os.path.join(radio_root, "SCRIPTS", "RF2", "rf2.lua")
            if not is_asset and os.path.isfile(rf2_lua_path):
                self.update_rf2_lua_version(rf2_lua_path, version_label)

            if not self.is_updating:
                return

            self.log("Final cleanup...")
            self.set_status("Cleaning up...")
            self.set_current_step("Cleanup")
            try:
                shutil.rmtree(temp_dir)
                temp_dir = None
            except Exception:
                pass
            self.mark_step_done("Cleanup")

            self.set_status("Update completed successfully!")
            self.progress_label.config(text="")
            self._stop_segment_pulse()
            self.log("")
            self.log("=" * 50)
            self.log("✓ UPDATE COMPLETED SUCCESSFULLY!")
            self.log("=" * 50)
            self.log("")
            full_version = self.read_rf2_lua_version(rf2_lua_path) if os.path.isfile(rf2_lua_path) else None
            if full_version:
                self.log(f"Installed version: {full_version}")
            else:
                self.log(f"Installed version label: {version_label}")
            self.log("You can now eject the SD card or restart the radio.")
            self.log("The new Rotorflight Lua scripts are ready to use.")

            messagebox.showinfo(
                "Update Complete",
                "Rotorflight Lua scripts have been updated successfully!\n\n"
                "You can now eject the SD card or restart the radio.",
            )
        except Exception as e:
            self.set_status("Update failed")
            self.log("")
            self.log("=" * 50)
            self.log(f"✗ UPDATE FAILED: {e}")
            self.log("=" * 50)

            messagebox.showerror(
                "Update Failed",
                f"The update process failed:\n\n{e}\n\nPlease check the log for details.",
            )
        finally:
            self.is_updating = False
            self._stop_segment_pulse()
            self._set_update_controls_running(False)
            if temp_dir and os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)


def check_dependencies():
    return True


def main():
    try:
        _clear_stale_lock_file()
        if os.path.exists(UPDATER_LOCK_FILE):
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showinfo("Updater Running", "The updater is already running.")
                root.destroy()
            except Exception:
                pass
            sys.exit(0)

        with open(UPDATER_LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(UPDATER_LOCK_FILE) and os.remove(UPDATER_LOCK_FILE))

        if not check_dependencies():
            sys.exit(1)

        root = tk.Tk()
        app = UpdaterGUI(root)

        def on_close():
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)
        root.mainloop()
    except Exception:
        error_log = WORK_DIR / "updater_error.log"
        with open(error_log, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Updater Error",
                f"Updater failed to start.\n\nDetails written to:\n{error_log}",
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

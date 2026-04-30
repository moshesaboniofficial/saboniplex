import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

import tdjson

from tdlib_saboniplex_maxspeed import (
    GROUP_TITLE,
    ensure_dirs,
    set_tdlib_parameters,
    td_send,
    td_receive,
    td_execute,
    plex_move,
    set_ui_sinks,
)


@dataclass
class DownloadItem:
    file_id: int
    name: str
    caption: str = ""
    status: str = "Queued"  # Queued | Downloading | Paused | Moving | Completed | Error
    total_size: int = 0
    downloaded_size: int = 0
    speed_bps: float = 0.0
    eta_sec: int = -1
    is_paused: bool = False
    local_path: str = ""
    created_ts: float = 0.0


class TdlibDownloadWorker(threading.Thread):
    """Runs TDLib receive loop on a background thread and emits UI events.

    Events are dicts put into event_queue.
    Commands are dicts consumed from command_queue.
    """

    def __init__(
        self,
        event_queue: "queue.Queue[dict]",
        command_queue: "queue.Queue[dict]",
        *,
        group_title: str = GROUP_TITLE,
    ):
        super().__init__(daemon=True)
        self._ev = event_queue
        self._cmd = command_queue
        self._group_title = group_title

        self._stop = threading.Event()
        self._client_id: Optional[int] = None
        self._authorized = False
        self._auth_waiting_for: Optional[str] = None  # phone|code|password

        self._chat_id: Optional[int] = None
        self._listening_enabled = False
        self._paused_all = True
        self._desired_running = False

        self._items: Dict[int, DownloadItem] = {}
        self._queue: List[int] = []
        self._current: Optional[int] = None

        self._last_bytes: Dict[int, int] = {}
        # _last_time: last time we observed *progress* (downloaded_size increased)
        self._last_time: Dict[int, float] = {}
        # _last_update_time: last time we received any update (including no-progress)
        self._last_update_time: Dict[int, float] = {}
        self._speed_ema: Dict[int, float] = {}

        # Progress smoothing / watchdog
        self._last_refresh: Dict[int, float] = {}
        self._stall_count: Dict[int, int] = {}
        self._stall_last_action: Dict[int, float] = {}

        self._refresh_interval_sec = 0.9
        self._stall_sec = 8.0
        self._stall_action_cooldown_sec = 3.0

        self._postprocess_lock = threading.Lock()

        # Concurrency: TDLib can download multiple files in parallel.
        # Default to 10 to improve throughput on connections where a single download is throttled.
        try:
            v = int(os.environ.get("SABONIPLEX_MAX_CONCURRENT_DOWNLOADS", "10"))
        except Exception:
            v = 10
        self._max_concurrent_downloads = max(1, min(12, v))

    # ----------------- helpers -----------------
    def _emit(self, typ: str, **payload: Any):
        self._ev.put({"type": typ, **payload})

    def _safe_log_sink(self, msg: str):
        # GUI might ignore logs; keep it as event.
        self._emit("log", message=str(msg))

    def _init_tdlib(self):
        ensure_dirs()
        self._client_id = tdjson.td_create_client_id()
        td_execute({"@type": "setLogVerbosityLevel", "new_verbosity_level": 1})
        td_send(self._client_id, {"@type": "getAuthorizationState"})

        # route plex_move logs to GUI if needed
        set_ui_sinks(log_sink=self._safe_log_sink, status_sink=None)

        self._emit("engine_state", state="initialized")

    def _toggle_all_paused(self, paused: bool):
        if not self._client_id:
            return
        td_send(self._client_id, {"@type": "toggleAllDownloadsArePaused", "are_paused": bool(paused)})
        self._paused_all = bool(paused)
        self._emit("engine_state", state="paused" if paused else "running")

    def _toggle_item_pause(self, file_id: int, paused: bool):
        if not self._client_id:
            return
        td_send(self._client_id, {"@type": "toggleDownloadIsPaused", "file_id": int(file_id), "is_paused": bool(paused)})

    def _request_download(self, file_id: int, priority: int = 32):
        if not self._client_id:
            return
        td_send(
            self._client_id,
            {
                "@type": "downloadFile",
                "file_id": int(file_id),
                "priority": int(priority),
                "offset": 0,
                "limit": 0,
                "synchronous": False,
            },
        )

    def _request_file_info(self, file_id: int):
        if not self._client_id:
            return
        td_send(self._client_id, {"@type": "getFile", "file_id": int(file_id)})

    def _start_listening(self):
        if not self._client_id or not self._chat_id:
            return
        td_send(self._client_id, {"@type": "setOption", "name": "online", "value": {"@type": "optionValueBoolean", "value": True}})
        td_send(self._client_id, {"@type": "openChat", "chat_id": int(self._chat_id)})
        self._listening_enabled = True
        self._emit("engine_state", state="listening")

    def _stop_listening(self):
        self._listening_enabled = False
        self._emit("engine_state", state="not_listening")

    def _find_chat_id_by_title(self, title: str) -> Optional[int]:
        if not self._client_id:
            return None

        td_send(self._client_id, {"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": 200})
        deadline = time.time() + 10
        chat_ids: List[int] = []

        while time.time() < deadline and not self._stop.is_set():
            upd = td_receive(0.5)
            if not upd:
                continue
            if upd.get("@type") == "chats":
                chat_ids = upd.get("chat_ids") or []
                break

        for cid in chat_ids:
            td_send(self._client_id, {"@type": "getChat", "chat_id": int(cid)})
            deadline2 = time.time() + 2
            while time.time() < deadline2 and not self._stop.is_set():
                r2 = td_receive(0.5)
                if not r2:
                    continue
                if r2.get("@type") == "chat" and r2.get("id") == cid:
                    if (r2.get("title") or "") == title:
                        return int(cid)
                    break
        return None

    def _next_unpaused_in_queue(self) -> Optional[int]:
        # Avoid infinite loop if everything is paused.
        n = len(self._queue)
        for _ in range(n):
            fid = self._queue.pop(0)
            it = self._items.get(fid)
            if not it:
                continue
            if it.status == "Completed":
                continue
            if it.is_paused:
                self._queue.append(fid)
                continue
            return fid
        return None

    def _set_current(self, file_id: Optional[int]):
        # In max-speed mode we allow multiple concurrent downloads.
        # _current is kept only as a "primary" item for UI/watchdog display.
        self._current = file_id

    def _active_download_count(self) -> int:
        return sum(1 for it in self._items.values() if it.status == "Downloading" and not it.is_paused)

    def _emit_item(self, file_id: int):
        it = self._items.get(file_id)
        if not it:
            return
        pct = 0.0
        if it.total_size > 0:
            pct = (it.downloaded_size / it.total_size) * 100.0
        eta_txt = "--:--"
        if it.eta_sec >= 0:
            eta_txt = f"{it.eta_sec // 60:02d}:{it.eta_sec % 60:02d}"

        def fmt_bytes(n: int) -> str:
            if n <= 0:
                return "0 B"
            units = [(1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")]
            for div, name in units:
                if n >= div:
                    return f"{n / div:.2f} {name}"
            return f"{n} B"

        speed_mb = it.speed_bps / 1024 / 1024
        self._emit(
            "item_updated",
            file_id=it.file_id,
            name=it.name,
            status=it.status,
            pct=pct,
            downloaded=fmt_bytes(it.downloaded_size),
            total=fmt_bytes(it.total_size),
            speed=f"{speed_mb:.2f} MB/s" if speed_mb > 0 else "0.00 MB/s",
            eta=eta_txt,
            queue_pos=(self._queue.index(it.file_id) + 1) if it.file_id in self._queue else (1 if it.file_id == self._current else ""),
        )

    def _maybe_start_next(self):
        if self._paused_all:
            return
        if not self._authorized or not self._client_id:
            return
        # Fill available slots up to the concurrency limit.
        while self._active_download_count() < self._max_concurrent_downloads:
            nxt = self._next_unpaused_in_queue()
            if nxt is None:
                return

            self._set_current(nxt)
            it = self._items.get(nxt)
            if not it:
                continue

            it.is_paused = False
            it.status = "Downloading"
            self._emit_item(nxt)
            self._request_download(nxt, priority=32)
            # ensure unpaused if TDLib has it paused
            self._toggle_item_pause(nxt, False)

            # prime progress tracking (TDLib updateFile can arrive in bursts)
            now = time.time()
            self._last_refresh[nxt] = 0.0
            self._last_time.setdefault(nxt, now)
            self._last_bytes.setdefault(nxt, 0)
            self._request_file_info(nxt)

    def _tick_progress(self):
        """Periodic progress refresh + stall watchdog for active downloads.

        TDLib sometimes emits updateFile in bursts; GUI then looks like it's stuck.
        We smooth this by polling getFile for active files.
        """
        if self._paused_all or not self._authorized or not self._client_id:
            return
        now = time.time()

        active_ids = [fid for fid, it in self._items.items() if it.status == "Downloading" and not it.is_paused]
        if not active_ids:
            return

        for fid in active_ids:
            it = self._items.get(fid)
            if not it:
                continue

            last_r = self._last_refresh.get(fid, 0.0)
            if (now - last_r) >= self._refresh_interval_sec:
                self._last_refresh[fid] = now
                self._request_file_info(fid)

            # Stall watchdog: if we haven't observed progress for a while, nudge TDLib.
            prev_t = self._last_time.get(fid)
            prev_b = self._last_bytes.get(fid)
            if prev_t is None or prev_b is None:
                continue
            if (now - prev_t) < self._stall_sec:
                continue

            # still no progress for stall window
            last_action = float(self._stall_last_action.get(fid, 0.0))
            if (now - last_action) < self._stall_action_cooldown_sec:
                continue

            n = int(self._stall_count.get(fid, 0)) + 1
            self._stall_count[fid] = n
            self._stall_last_action[fid] = now
            if n <= 3:
                self._emit(
                    "log",
                    message=f"STALL watchdog: no progress for {int(now - prev_t)}s (file_id={fid}) -> re-request download",
                )
                self._request_download(fid, priority=32)
                self._toggle_item_pause(fid, False)

    def _handle_new_media(self, file_id: int, suggested: str, caption: str = ""):
        if file_id in self._items:
            return
        it = DownloadItem(file_id=int(file_id), name=str(suggested), caption=str(caption or ""), created_ts=time.time())
        self._items[it.file_id] = it
        self._queue.append(it.file_id)
        self._emit(
            "item_added",
            file_id=it.file_id,
            name=it.name,
        )
        self._emit_item(it.file_id)
        self._maybe_start_next()

    def _handle_update_file(self, f: dict):
        file_id = f.get("id")
        if not file_id:
            return
        file_id = int(file_id)
        it = self._items.get(file_id)
        if not it:
            return

        local = f.get("local") or {}
        it.total_size = int(f.get("size") or 0)
        it.downloaded_size = int(local.get("downloaded_size") or 0)
        it.local_path = str(local.get("path") or "")

        now = time.time()
        prev_u = self._last_update_time.get(file_id, now)
        prev_b = self._last_bytes.get(file_id, it.downloaded_size)
        dt = max(1e-6, now - prev_u)
        db = max(0, it.downloaded_size - prev_b)
        speed_raw = db / dt
        prev_speed = self._speed_ema.get(file_id, speed_raw)
        speed = 0.85 * prev_speed + 0.15 * speed_raw
        self._speed_ema[file_id] = speed
        it.speed_bps = speed

        if it.total_size > 0 and it.speed_bps > 0:
            it.eta_sec = int(max(0, it.total_size - it.downloaded_size) / it.speed_bps)
        else:
            it.eta_sec = -1

        self._last_update_time[file_id] = now
        self._last_bytes[file_id] = it.downloaded_size
        if db > 0:
            # progress happened: reset stall counters and mark last-progress time
            self._last_time[file_id] = now
            self._stall_count[file_id] = 0

        if bool(local.get("is_downloading_completed")):
            it.status = "Moving"
            it.is_paused = False
            self._emit_item(file_id)

            # if the primary completed, pick another active item (if any)
            if self._current == file_id:
                self._current = next(
                    (fid for fid, it2 in self._items.items() if fid != file_id and it2.status == "Downloading" and not it2.is_paused),
                    None,
                )

            self._start_postprocess(file_id)
            self._maybe_start_next()
        else:
            # keep status coherent
            if it.is_paused and it.status != "Paused" and it.status not in ("Completed", "Moving"):
                it.status = "Paused"
            if (not it.is_paused) and bool(local.get("is_downloading_active")) and it.status not in ("Downloading", "Moving", "Completed"):
                it.status = "Downloading"
            self._emit_item(file_id)

    def _start_postprocess(self, file_id: int):
        it = self._items.get(file_id)
        if not it:
            return

        def job():
            try:
                src = it.local_path
                if src and os.path.exists(src):
                    official_name = plex_move(src, it.name, it.caption)
                    if official_name:
                        it.name = str(official_name)
                        it.status = "Completed"
                    else:
                        it.status = "Error"
                else:
                    it.status = "Error"
            except Exception:
                it.status = "Error"
            self._emit_item(file_id)

        t = threading.Thread(target=job, daemon=True)
        t.start()

    # ----------------- command handling -----------------
    def _handle_command(self, cmd: dict):
        c = cmd.get("cmd")

        if c == "clear_completed":
            to_remove = [fid for fid, it in self._items.items() if it.status == "Completed"]
            if not to_remove:
                return

            self._queue = [x for x in self._queue if x not in to_remove]
            if self._current in to_remove:
                self._current = None

            for fid in to_remove:
                self._items.pop(fid, None)
                self._last_bytes.pop(fid, None)
                self._last_time.pop(fid, None)
                self._last_update_time.pop(fid, None)
                self._speed_ema.pop(fid, None)
                self._last_refresh.pop(fid, None)
                self._stall_count.pop(fid, None)
                self._stall_last_action.pop(fid, None)

            self._emit("items_removed", file_ids=to_remove)
            return

        if c == "shutdown":
            self._stop.set()
            return

        if c == "start_all":
            self._desired_running = True
            self._toggle_all_paused(False)
            if self._chat_id:
                self._start_listening()
            self._maybe_start_next()
            return

        if c == "stop_all":
            self._desired_running = False
            self._stop_listening()
            self._toggle_all_paused(True)
            # reflect status locally
            for fid, it in self._items.items():
                if it.status == "Downloading":
                    it.is_paused = True
                    it.status = "Paused"
                    self._emit_item(fid)
            self._current = None
            return

        if c == "auth_phone":
            phone = (cmd.get("value") or "").strip()
            if phone and self._client_id:
                td_send(self._client_id, {"@type": "setAuthenticationPhoneNumber", "phone_number": phone})
            return

        if c == "auth_code":
            code = (cmd.get("value") or "").strip()
            if code and self._client_id:
                td_send(self._client_id, {"@type": "checkAuthenticationCode", "code": code})
            return

        if c == "auth_password":
            pwd = (cmd.get("value") or "").strip()
            if pwd and self._client_id:
                td_send(self._client_id, {"@type": "checkAuthenticationPassword", "password": pwd})
            return

        if c == "pause_item":
            fid = int(cmd.get("file_id"))
            it = self._items.get(fid)
            if not it:
                return
            it.is_paused = True
            it.status = "Paused"
            self._toggle_item_pause(fid, True)
            # fill any freed slot
            if self._current == fid:
                self._current = None
            self._maybe_start_next()
            self._emit_item(fid)
            return

        if c == "resume_item":
            fid = int(cmd.get("file_id"))
            it = self._items.get(fid)
            if not it:
                return
            if self._paused_all:
                # user wants to resume this -> unpause all
                self._toggle_all_paused(False)

            # remove from queue if present
            if fid in self._queue:
                self._queue = [x for x in self._queue if x != fid]
            self._set_current(fid)

            it.is_paused = False
            it.status = "Downloading"
            self._emit_item(fid)

            self._request_download(fid, priority=32)
            self._toggle_item_pause(fid, False)

            # also start other queued items up to the concurrency limit
            self._maybe_start_next()
            return

    # ----------------- update handling -----------------
    def _handle_auth_state(self, state: str):
        if not self._client_id:
            return

        if state == "authorizationStateWaitTdlibParameters":
            set_tdlib_parameters(self._client_id)
            td_send(self._client_id, {"@type": "checkDatabaseEncryptionKey", "encryption_key": ""})
            return

        if state == "authorizationStateWaitPhoneNumber":
            self._auth_waiting_for = "phone"
            self._emit("auth_request", kind="phone")
            return

        if state == "authorizationStateWaitCode":
            self._auth_waiting_for = "code"
            self._emit("auth_request", kind="code")
            return

        if state == "authorizationStateWaitPassword":
            self._auth_waiting_for = "password"
            self._emit("auth_request", kind="password")
            return

        if state == "authorizationStateReady":
            self._authorized = True
            self._auth_waiting_for = None
            self._emit("auth_ready")

            # resolve chat id once
            self._emit("engine_state", state="finding_chat")
            cid = self._find_chat_id_by_title(self._group_title)
            if not cid:
                self._emit("engine_state", state="error", message=f'Chat "{self._group_title}" not found')
                return
            self._chat_id = cid
            self._emit("engine_state", state="chat_ready", chat_id=cid)
            # apply desired running state if Start was pressed early
            if self._desired_running:
                self._toggle_all_paused(False)
                self._start_listening()
                self._maybe_start_next()
            return

        if state == "authorizationStateClosed":
            self._emit("engine_state", state="error", message="TDLib authorization closed")
            return

    def run(self):
        self._init_tdlib()

        while not self._stop.is_set():
            # process commands quickly
            while True:
                try:
                    cmd = self._cmd.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_command(cmd)
                except Exception as e:
                    self._emit("engine_state", state="error", message=str(e))

            if self._stop.is_set():
                break

            # periodic refresh/watchdog (independent of update bursts)
            try:
                self._tick_progress()
            except Exception:
                # never let the watchdog kill the worker
                pass

            upd = td_receive(0.2)
            if not upd:
                continue

            ut = upd.get("@type")

            if ut == "updateAuthorizationState":
                st = (upd.get("authorization_state") or {}).get("@type")
                if st:
                    self._handle_auth_state(st)
                continue

            if not self._authorized:
                continue

            if ut == "updateNewMessage":
                if not self._listening_enabled or not self._chat_id:
                    continue
                msg = upd.get("message") or {}
                if msg.get("chat_id") != self._chat_id:
                    continue

                content = msg.get("content") or {}
                ctype = content.get("@type")
                file_obj = None
                suggested = None
                caption_text = ""

                cap = content.get("caption") or {}
                if isinstance(cap, dict):
                    caption_text = str(cap.get("text") or "")

                if ctype == "messageDocument":
                    doc = content.get("document") or {}
                    file_obj = doc.get("document")
                    suggested = (doc.get("file_name") or "video")
                elif ctype == "messageVideo":
                    vid = content.get("video") or {}
                    file_obj = vid.get("video")
                    suggested = (vid.get("file_name") or "video")

                if not file_obj:
                    continue

                file_id = file_obj.get("id")
                if not file_id:
                    continue

                self._handle_new_media(int(file_id), str(suggested), caption_text)
                continue

            if ut == "updateFile":
                f = upd.get("file") or {}
                self._handle_update_file(f)
                continue

            # Responses from getFile are plain "file" objects
            if ut == "file":
                self._handle_update_file(upd)
                continue

        # shutdown
        try:
            if self._client_id:
                td_send(self._client_id, {"@type": "close"})
        except Exception:
            pass
        self._emit("engine_state", state="stopped")

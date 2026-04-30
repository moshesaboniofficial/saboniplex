import queue
import tkinter as tk
from tkinter import ttk
import time

from saboniplex_engine import TdlibDownloadWorker


STATUS_HE = {
    "Queued": "בתור",
    "Downloading": "מוריד",
    "Paused": "מושהה",
    "Moving": "מעביר ל‑Plex",
    "Completed": "הושלם",
    "Error": "שגיאה",
}


ENGINE_STATE_HE = {
    "initialized": "מאותחל",
    "finding_chat": "מחפש קבוצה…",
    "chat_ready": "הקבוצה מוכנה",
    "listening": "מקשיב לקבוצה",
    "not_listening": "לא מקשיב",
    "running": "רץ",
    "paused": "מושהה",
    "stopped": "נעצר",
    "error": "שגיאה",
}


class SaboniPlexApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SaboniPlex")
        self.root.geometry("980x520")
        self.root.minsize(860, 480)

        self.events: "queue.Queue[dict]" = queue.Queue()
        self.commands: "queue.Queue[dict]" = queue.Queue()

        self.worker = TdlibDownloadWorker(self.events, self.commands)
        self.worker.start()

        self._engine_status_text = ""
        self._last_log_ts = 0.0

        self._build_ui()
        self._poll_events()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        top.pack(side=tk.TOP, fill=tk.X)

        # RTL-ish: place primary controls on the right
        self.btn_stop_all = ttk.Button(top, text="עצור", command=self._stop_all)
        self.btn_start_all = ttk.Button(top, text="התחל", command=self._start_all)
        self.btn_clear_completed = ttk.Button(top, text="נקה רשימה", command=self._clear_completed)
        self.btn_stop_all.pack(side=tk.RIGHT)
        self.btn_start_all.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_clear_completed.pack(side=tk.RIGHT, padx=(0, 8))

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X, padx=10)

        # Auth panel (shows only when needed)
        self.auth_frame = ttk.Labelframe(self.root, text="התחברות לטלגרם", padding=(10, 8))
        self.auth_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 0))

        self.auth_label = ttk.Label(self.auth_frame, text="ממתין…", anchor=tk.E, justify=tk.RIGHT)
        self.auth_label.pack(side=tk.RIGHT)

        self.auth_entry = ttk.Entry(self.auth_frame, width=40)
        try:
            self.auth_entry.configure(justify="right")
        except Exception:
            pass
        self.auth_entry.pack(side=tk.RIGHT, padx=(0, 8))

        self.auth_btn = ttk.Button(self.auth_frame, text="שלח", command=self._submit_auth)
        self.auth_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self._auth_kind = None
        self._set_auth_visible(False)

        content = ttk.Frame(self.root, padding=(10, 10, 10, 8))
        content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        mid = ttk.Frame(content)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # RTL visual order: first column appears on the left in Tk, so we reverse logical order
        # so that "שם" ends up on the rightmost side.
        columns = ("q", "eta", "speed", "size", "pct", "status", "name")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("q", text="תור")
        self.tree.heading("eta", text="זמן שנותר")
        self.tree.heading("speed", text="מהירות")
        self.tree.heading("size", text="התקדמות")
        self.tree.heading("pct", text="%")
        self.tree.heading("status", text="מצב")
        self.tree.heading("name", text="שם")

        # Column sizing (physical left->right order is: q, eta, speed, size, pct, status, name)
        self.tree.column("q", width=55, anchor=tk.CENTER)
        self.tree.column("eta", width=95, anchor=tk.E)
        self.tree.column("speed", width=115, anchor=tk.E)
        self.tree.column("size", width=200, anchor=tk.E)
        self.tree.column("pct", width=70, anchor=tk.E)
        self.tree.column("status", width=120, anchor=tk.E)
        self.tree.column("name", width=340, anchor=tk.E)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        scroll = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Details panel (selected item)
        details = ttk.Labelframe(content, text="פרטים", padding=(10, 8))
        details.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        self.sel_name_var = tk.StringVar(value="בחר פריט כדי לראות פרטים")
        self.sel_status_var = tk.StringVar(value="")
        self.sel_size_var = tk.StringVar(value="")
        self.sel_speed_var = tk.StringVar(value="")
        self.sel_eta_var = tk.StringVar(value="")

        name_row = ttk.Frame(details)
        name_row.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(name_row, textvariable=self.sel_name_var, anchor=tk.E, justify=tk.RIGHT).pack(
            side=tk.RIGHT, fill=tk.X, expand=True
        )

        stats_row = ttk.Frame(details)
        stats_row.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
        ttk.Label(stats_row, textvariable=self.sel_eta_var, width=14, anchor=tk.E, justify=tk.RIGHT).pack(side=tk.RIGHT)
        ttk.Label(stats_row, textvariable=self.sel_speed_var, width=18, anchor=tk.E, justify=tk.RIGHT).pack(side=tk.RIGHT, padx=(0, 10))
        ttk.Label(stats_row, textvariable=self.sel_size_var, width=30, anchor=tk.E, justify=tk.RIGHT).pack(side=tk.RIGHT, padx=(0, 10))
        ttk.Label(stats_row, textvariable=self.sel_status_var, width=18, anchor=tk.E, justify=tk.RIGHT).pack(side=tk.RIGHT, padx=(0, 10))

        self.sel_progress = ttk.Progressbar(details, orient=tk.HORIZONTAL, mode="determinate", maximum=100)
        self.sel_progress.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X, padx=10)

        bottom = ttk.Frame(self.root, padding=(10, 8))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)

        self.btn_pause_sel = ttk.Button(bottom, text="עצור נבחר", command=self._pause_selected)
        self.btn_resume_sel = ttk.Button(bottom, text="התחל נבחר", command=self._resume_selected)
        self.btn_pause_sel.pack(side=tk.RIGHT)
        self.btn_resume_sel.pack(side=tk.RIGHT, padx=(0, 8))

        self.status_var = tk.StringVar(value="מאתחל…")
        self.status_lbl = ttk.Label(bottom, textvariable=self.status_var)
        self.status_lbl.pack(side=tk.LEFT)

        # Map file_id -> tree iid (iid is str(file_id))
        self._known_items = set()
        self._item_cache = {}

    def _set_auth_visible(self, visible: bool):
        if visible:
            self.auth_frame.pack_configure(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 0))
        else:
            self.auth_frame.pack_forget()

    def _on_select(self, _evt=None):
        fid = self._selected_file_id()
        if fid is None:
            self._render_details(None)
            return
        self._render_details(fid)

    def _render_details(self, file_id):
        if file_id is None:
            self.sel_name_var.set("בחר פריט כדי לראות פרטים")
            self.sel_status_var.set("")
            self.sel_size_var.set("")
            self.sel_speed_var.set("")
            self.sel_eta_var.set("")
            self.sel_progress["value"] = 0
            return

        data = self._item_cache.get(int(file_id))
        if not data:
            self.sel_name_var.set(str(file_id))
            self.sel_status_var.set("")
            self.sel_size_var.set("")
            self.sel_speed_var.set("")
            self.sel_eta_var.set("")
            self.sel_progress["value"] = 0
            return

        self.sel_name_var.set(data.get("name") or "")
        self.sel_status_var.set(f"מצב: {data.get('status') or ''}")
        self.sel_size_var.set(f"התקדמות: {data.get('downloaded') or ''} / {data.get('total') or ''}")
        self.sel_speed_var.set(f"מהירות: {data.get('speed') or ''}")
        self.sel_eta_var.set(f"נשאר: {data.get('eta') or ''}")
        try:
            self.sel_progress["value"] = float(data.get("pct") or 0.0)
        except Exception:
            self.sel_progress["value"] = 0

    def _start_all(self):
        self.commands.put({"cmd": "start_all"})

    def _stop_all(self):
        self.commands.put({"cmd": "stop_all"})

    def _clear_completed(self):
        self.commands.put({"cmd": "clear_completed"})

    def _selected_file_id(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except Exception:
            return None

    def _resume_selected(self):
        fid = self._selected_file_id()
        if fid is None:
            return
        self.commands.put({"cmd": "resume_item", "file_id": fid})

    def _pause_selected(self):
        fid = self._selected_file_id()
        if fid is None:
            return
        self.commands.put({"cmd": "pause_item", "file_id": fid})

    def _submit_auth(self):
        val = self.auth_entry.get()
        if not self._auth_kind:
            return
        if self._auth_kind == "phone":
            self.commands.put({"cmd": "auth_phone", "value": val})
        elif self._auth_kind == "code":
            self.commands.put({"cmd": "auth_code", "value": val})
        elif self._auth_kind == "password":
            self.commands.put({"cmd": "auth_password", "value": val})
        self.auth_entry.delete(0, tk.END)

    def _poll_events(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(ev)
        self.root.after(120, self._poll_events)

    def _handle_event(self, ev: dict):
        typ = ev.get("type")

        if typ == "engine_state":
            st = ev.get("state")
            msg = ev.get("message")
            st_he = ENGINE_STATE_HE.get(str(st), str(st))
            if msg:
                self._engine_status_text = f"{st_he}: {msg}"
            else:
                self._engine_status_text = str(st_he)

            self.status_var.set(self._engine_status_text)
            return

        if typ == "log":
            # Minimal: surface logs in the status bar without adding new UI panels.
            message = str(ev.get("message") or "").strip()
            if not message:
                return

            now = time.time()
            if (now - self._last_log_ts) < 0.15:
                return
            self._last_log_ts = now

            base = self._engine_status_text or self.status_var.get() or ""
            if base:
                text = f"{base} · {message}"
            else:
                text = message

            # Prevent an endlessly long status line
            if len(text) > 220:
                text = text[:217] + "..."
            self.status_var.set(text)
            return

        if typ == "auth_request":
            kind = ev.get("kind")
            self._auth_kind = kind
            self._set_auth_visible(True)
            if kind == "phone":
                self.auth_label.config(text="הכנס מספר טלפון (+972...):")
            elif kind == "code":
                self.auth_label.config(text="הכנס קוד מטלגרם:")
            elif kind == "password":
                self.auth_label.config(text="הכנס סיסמת אימות דו‑שלבי:")
            else:
                self.auth_label.config(text="הכנס ערך:")
            self.auth_entry.focus_set()
            return

        if typ == "auth_ready":
            self._auth_kind = None
            self._set_auth_visible(False)
            return

        if typ == "item_added":
            fid = int(ev.get("file_id"))
            name = ev.get("name") or "video"
            iid = str(fid)
            if iid not in self._known_items:
                self._known_items.add(iid)
                self.tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    # physical order: q, eta, speed, size, pct, status, name
                    values=("", "--:--", "0.00 MB/s", "0 B / 0 B", "0.0", STATUS_HE.get("Queued", "Queued"), name),
                )
            return

        if typ == "item_updated":
            fid = int(ev.get("file_id"))
            iid = str(fid)
            name = ev.get("name") or "video"
            status_raw = ev.get("status") or "Queued"
            status = STATUS_HE.get(str(status_raw), str(status_raw))
            pct = ev.get("pct")
            downloaded = ev.get("downloaded") or "0 B"
            total = ev.get("total") or "0 B"
            speed = ev.get("speed") or "0.00 MB/s"
            eta = ev.get("eta") or "--:--"
            qpos = ev.get("queue_pos")

            self._item_cache[fid] = {
                "name": name,
                "status": status,
                "pct": pct,
                "downloaded": downloaded,
                "total": total,
                "speed": speed,
                "eta": eta,
                "queue_pos": qpos,
            }

            if iid not in self._known_items:
                self._known_items.add(iid)
                self.tree.insert("", tk.END, iid=iid, values=("", "", "", "", "", "", ""))

            status_cell = str(status)
            pct_cell = f"{pct:5.1f}" if isinstance(pct, (int, float)) else str(pct or "0.0")
            size_cell = f"{downloaded} / {total}"
            q_cell = str(qpos) if qpos else ""

            self.tree.item(iid, values=(q_cell, eta, speed, size_cell, pct_cell, status_cell, name))

            sel = self._selected_file_id()
            if sel == fid:
                self._render_details(fid)
            return

        if typ == "items_removed":
            ids = ev.get("file_ids") or []
            removed_any = False
            for fid in ids:
                try:
                    iid = str(int(fid))
                except Exception:
                    continue
                if iid in self._known_items:
                    self._known_items.discard(iid)
                try:
                    self.tree.delete(iid)
                    removed_any = True
                except Exception:
                    pass
                try:
                    self._item_cache.pop(int(fid), None)
                except Exception:
                    pass

            if removed_any:
                self._render_details(self._selected_file_id())
            return

        # ignore other events (log etc)

    def _on_close(self):
        self.commands.put({"cmd": "shutdown"})
        self.root.after(200, self.root.destroy)


def main():
    root = tk.Tk()
    # ttk theme (classic-ish)
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        # Subtle visual improvements without custom colors
        try:
            style.configure("Treeview", rowheight=24)
            style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        except Exception:
            pass
    except Exception:
        pass
    SaboniPlexApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

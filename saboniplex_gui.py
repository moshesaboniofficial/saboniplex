import queue
import tkinter as tk
from tkinter import ttk, simpledialog
import time
import os
import json

from saboniplex_engine import TdlibDownloadWorker
from saboniplex_settings import load_settings

OVERRIDES_PATH = r"C:\\SaboniPlex\\classification_overrides.json"
NEEDS_REVIEW_DIR = r"F:\\Movies\\_Needs Review"


def _env_get(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val is not None:
        return str(val)
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
        except Exception:
            pass
    return default


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
        self.root.geometry("1040x580")
        self.root.minsize(920, 520)

        self.events: "queue.Queue[dict]" = queue.Queue()
        self.commands: "queue.Queue[dict]" = queue.Queue()
        self._settings = load_settings()
        self._notifications_enabled = bool(self._settings.get("notifications_enabled", True))
        self._notify_on_error = bool(self._settings.get("notify_on_error", True))
        self._notify_on_complete = bool(self._settings.get("notify_on_complete", False))

        self.worker = TdlibDownloadWorker(self.events, self.commands)
        self.worker.start()

        self._engine_status_text = ""
        self._last_log_ts = 0.0
        self._ai_status_text = self._compute_ai_status()

        self._apply_telegram_theme()
        self._build_ui()
        self._poll_events()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        top.pack(side=tk.TOP, fill=tk.X)

        # RTL-ish: place primary controls on the right
        self.btn_stop_all = ttk.Button(top, text="עצור", command=self._stop_all, style="TG.TButton")
        self.btn_start_all = ttk.Button(top, text="התחל", command=self._start_all, style="TG.TButton")
        self.btn_clear_completed = ttk.Button(top, text="נקה רשימה", command=self._clear_completed, style="TG.TButton")
        self.btn_stop_all.pack(side=tk.RIGHT)
        self.btn_start_all.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_clear_completed.pack(side=tk.RIGHT, padx=(0, 8))

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X, padx=10)

        # Auth panel (shows only when needed)
        self.auth_frame = ttk.Labelframe(self.root, text="התחברות לטלגרם", padding=(10, 8))
        self.auth_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 0))

        self.auth_label = ttk.Label(self.auth_frame, text="ממתין…", anchor=tk.E, justify=tk.RIGHT)
        self.auth_label.pack(side=tk.RIGHT)

        self.auth_entry = ttk.Entry(self.auth_frame, width=40, style="TG.TEntry")
        try:
            self.auth_entry.configure(justify="right")
        except Exception:
            pass
        self.auth_entry.pack(side=tk.RIGHT, padx=(0, 8))

        self.auth_btn = ttk.Button(self.auth_frame, text="שלח", command=self._submit_auth, style="TG.TButton")
        self.auth_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self._auth_kind = None
        self._set_auth_visible(False)

        content = ttk.Frame(self.root, padding=(10, 10, 10, 8))
        content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        filter_row = ttk.Frame(content)
        filter_row.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self.view_mode = tk.StringVar(value="all")
        ttk.Radiobutton(filter_row, text="All", value="all", variable=self.view_mode, command=self._refresh_tree_filter, style="TG.TRadiobutton").pack(side=tk.RIGHT)
        ttk.Radiobutton(filter_row, text="Failed", value="failed", variable=self.view_mode, command=self._refresh_tree_filter, style="TG.TRadiobutton").pack(side=tk.RIGHT, padx=(0, 8))

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
        self.sel_decision_var = tk.StringVar(value="")

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
        ttk.Label(details, textvariable=self.sel_decision_var, anchor=tk.E, justify=tk.RIGHT).pack(side=tk.TOP, fill=tk.X, pady=(6, 0))

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X, padx=10)

        bottom = ttk.Frame(self.root, padding=(10, 8))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)

        self.btn_pause_sel = ttk.Button(bottom, text="עצור נבחר", command=self._pause_selected, style="TG.TButton")
        self.btn_resume_sel = ttk.Button(bottom, text="התחל נבחר", command=self._resume_selected, style="TG.TButton")
        self.btn_pause_sel.pack(side=tk.RIGHT)
        self.btn_resume_sel.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_override_sel = ttk.Button(bottom, text="Create Override", command=self._create_override_for_selected, style="TG.TButton")
        self.btn_override_sel.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_open_overrides = ttk.Button(bottom, text="Open Overrides File", command=self._open_overrides_file, style="TG.TButton")
        self.btn_open_overrides.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_retry_failed = ttk.Button(bottom, text="Retry Failed", command=self._retry_failed, style="TG.TButton")
        self.btn_retry_failed.pack(side=tk.RIGHT, padx=(0, 8))
        self.btn_undo_move = ttk.Button(bottom, text="Undo Last Move", command=self._undo_last_move, style="TG.TButton")
        self.btn_undo_move.pack(side=tk.RIGHT, padx=(0, 8))

        self.status_var = tk.StringVar(value="מאתחל…")
        self.status_lbl = ttk.Label(bottom, textvariable=self.status_var, style="TG.Status.TLabel")
        self.status_lbl.pack(side=tk.LEFT)
        self.health_var = tk.StringVar(value="")
        self.health_lbl = ttk.Label(bottom, textvariable=self.health_var, style="TG.Status.TLabel")
        self.health_lbl.pack(side=tk.LEFT, padx=(12, 0))
        self.ai_var = tk.StringVar(value=f"AI: {self._ai_status_text}")
        self.ai_lbl = ttk.Label(bottom, textvariable=self.ai_var, style="TG.Status.TLabel")
        self.ai_lbl.pack(side=tk.LEFT, padx=(12, 0))

        # Map file_id -> tree iid (iid is str(file_id))
        self._known_items = set()
        self._item_cache = {}
        self._last_status_by_id = {}
        self._raw_status_by_id = {}
        self._review_index = []

        review = ttk.Labelframe(content, text="Needs Review", padding=(10, 8))
        review.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        self.review_list = tk.Listbox(review, height=5)
        self.review_list.pack(side=tk.TOP, fill=tk.X)
        review_btns = ttk.Frame(review)
        review_btns.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
        ttk.Button(review_btns, text="Approve", command=self._approve_review_selected, style="TG.TButton").pack(side=tk.RIGHT)
        ttk.Button(review_btns, text="Fix", command=self._fix_review_selected, style="TG.TButton").pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(review_btns, text="Retry with AI", command=self._retry_review_selected, style="TG.TButton").pack(side=tk.RIGHT, padx=(0, 8))

    def _compute_ai_status(self) -> str:
        if _env_get("GROQ_API_KEY", "").strip():
            return "TMDB + GROQ"
        if _env_get("OPENAI_API_KEY", "").strip():
            return "TMDB + OPENAI"
        return "TMDB only"

    def _refresh_ai_status(self):
        self._ai_status_text = self._compute_ai_status()
        self.ai_var.set(f"AI: {self._ai_status_text}")

    def _set_auth_visible(self, visible: bool):
        if visible:
            self.auth_frame.pack_configure(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 0))
        else:
            self.auth_frame.pack_forget()

    def _apply_telegram_theme(self):
        # Restore classic/native ttk look (no custom color palette).
        style = ttk.Style()
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
            elif "winnative" in style.theme_names():
                style.theme_use("winnative")
            elif "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass

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
            self.sel_decision_var.set("")
            self.sel_progress["value"] = 0
            return

        data = self._item_cache.get(int(file_id))
        if not data:
            self.sel_name_var.set(str(file_id))
            self.sel_status_var.set("")
            self.sel_size_var.set("")
            self.sel_speed_var.set("")
            self.sel_eta_var.set("")
            self.sel_decision_var.set("")
            self.sel_progress["value"] = 0
            return

        self.sel_name_var.set(data.get("name") or "")
        self.sel_status_var.set(f"מצב: {data.get('status') or ''}")
        self.sel_size_var.set(f"התקדמות: {data.get('downloaded') or ''} / {data.get('total') or ''}")
        self.sel_speed_var.set(f"מהירות: {data.get('speed') or ''}")
        self.sel_eta_var.set(f"נשאר: {data.get('eta') or ''}")

        reason = data.get("classify_reason") or ""
        conf = data.get("classify_confidence")
        tmdb_used = bool(data.get("classify_tmdb_used"))
        ai_used = bool(data.get("classify_ai_used"))
        ai_conf = data.get("classify_ai_confidence")
        try:
            conf_txt = f"{float(conf):.2f}"
        except Exception:
            conf_txt = ""
        try:
            ai_conf_txt = f"{float(ai_conf):.2f}"
        except Exception:
            ai_conf_txt = ""
        if reason:
            src = "TMDB" if tmdb_used else "Rules"
            if ai_used:
                src += "+AI"
            ai_suffix = f" | ai_conf {ai_conf_txt}" if ai_conf_txt else ""
            self.sel_decision_var.set(f"Classification: {reason}" + (f" (confidence {conf_txt})" if conf_txt else "") + f" | source {src}{ai_suffix}")
        else:
            self.sel_decision_var.set("")
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

    def _retry_failed(self):
        self.commands.put({"cmd": "retry_failed"})

    def _undo_last_move(self):
        self.commands.put({"cmd": "undo_last_move"})

    def _review_selected_file_id(self):
        sel = self.review_list.curselection()
        if not sel:
            return None
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._review_index):
            return None
        return int(self._review_index[idx])

    def _refresh_review_queue(self):
        self._review_index = []
        self.review_list.delete(0, tk.END)
        for fid, data in sorted(self._item_cache.items(), key=lambda x: x[0], reverse=True):
            reason = str(data.get("classify_reason") or "")
            dest = str(data.get("classify_dest_path") or "")
            status_raw = str(self._raw_status_by_id.get(fid) or "")
            if status_raw != "Completed":
                continue
            if ("needs_review" not in reason.lower()) and (NEEDS_REVIEW_DIR.lower() not in dest.lower()):
                continue
            name = str(data.get("name") or f"item {fid}")
            conf = data.get("classify_confidence") or 0.0
            try:
                conf_txt = f"{float(conf):.2f}"
            except Exception:
                conf_txt = "0.00"
            self._review_index.append(fid)
            self.review_list.insert(tk.END, f"{fid} | {name} | {reason} | conf {conf_txt}")

    def _approve_review_selected(self):
        fid = self._review_selected_file_id()
        if fid is None:
            self.status_var.set("Select a review item first")
            return
        data = self._item_cache.get(int(fid)) or {}
        base_name = str(data.get("name") or "").strip() or f"item {fid}"
        m = simpledialog.askstring("Approve", "Movie/Series name (English):", initialvalue=base_name, parent=self.root)
        if not m:
            return
        self._open_override_editor(fid, m.strip(), reprocess_after_save=True)
        self.status_var.set(f"Approve opened for item {fid}")

    def _fix_review_selected(self):
        fid = self._review_selected_file_id()
        if fid is None:
            self.status_var.set("Select a review item first")
            return
        data = self._item_cache.get(int(fid)) or {}
        suggested_name = str(data.get("name") or "").strip()
        self._open_override_editor(fid, suggested_name, reprocess_after_save=True)

    def _retry_review_selected(self):
        fid = self._review_selected_file_id()
        if fid is None:
            self.status_var.set("Select a review item first")
            return
        self.commands.put({"cmd": "reprocess_item", "file_id": int(fid)})
        self.status_var.set(f"Retry with AI queued for item {fid}")


    def _load_overrides(self):
        try:
            if not os.path.exists(OVERRIDES_PATH):
                return []
            with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            return []
        except Exception:
            return []

    def _save_overrides(self, rows):
        parent = os.path.dirname(OVERRIDES_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)

    def _create_override_for_selected(self):
        fid = self._selected_file_id()
        if fid is None:
            self.status_var.set("Select an item first")
            return
        data = self._item_cache.get(int(fid)) or {}
        suggested_name = str(data.get("name") or "").strip()
        self._open_override_editor(fid, suggested_name)

    def _open_overrides_file(self):
        try:
            if not os.path.exists(OVERRIDES_PATH):
                self._save_overrides([])
            os.startfile(OVERRIDES_PATH)
            self.status_var.set("Opened overrides file")
        except Exception as ex:
            self.status_var.set(f"Failed to open overrides: {ex}")

    def _open_override_editor(self, fid: int, suggested_name: str, *, reprocess_after_save: bool = False):
        win = tk.Toplevel(self.root)
        win.title("Create Override")
        win.geometry("520x360")
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        kind_var = tk.StringVar(value="movie")
        match_var = tk.StringVar(value=suggested_name)
        name_var = tk.StringVar(value=suggested_name)
        year_var = tk.StringVar(value="")
        root_var = tk.StringVar(value="movies")
        season_var = tk.StringVar(value="1")
        episode_var = tk.StringVar(value="1")

        ttk.Label(frm, text="match").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(frm, textvariable=match_var, width=42).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="kind").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Combobox(frm, textvariable=kind_var, values=["movie", "series"], state="readonly", width=12, style="TG.TCombobox").grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="name").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(frm, textvariable=name_var, width=42).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="year").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(frm, textvariable=year_var, width=12).grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="root").grid(row=4, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Combobox(frm, textvariable=root_var, values=["movies", "kids", "israeli"], state="readonly", width=12, style="TG.TCombobox").grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="season").grid(row=5, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(frm, textvariable=season_var, width=12).grid(row=5, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="episode").grid(row=6, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(frm, textvariable=episode_var, width=12).grid(row=6, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="movie: name/year/root | series: name/season/episode").grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 10))

        def on_save():
            match = (match_var.get() or "").strip()
            kind = (kind_var.get() or "").strip().lower()
            name = (name_var.get() or "").strip()
            if not match or not name or kind not in ("movie", "series"):
                self.status_var.set("Override invalid: fill match/kind/name")
                return

            row = {"match": match, "kind": kind, "name": name}
            if kind == "movie":
                yy = (year_var.get() or "").strip()
                root = (root_var.get() or "movies").strip().lower()
                if yy:
                    if not (len(yy) == 4 and yy.isdigit()):
                        self.status_var.set("Invalid year (use YYYY)")
                        return
                    row["year"] = yy
                if root not in ("movies", "kids", "israeli"):
                    root = "movies"
                row["root"] = root
            else:
                try:
                    s = int((season_var.get() or "1").strip())
                    e = int((episode_var.get() or "1").strip())
                except Exception:
                    self.status_var.set("Invalid season/episode")
                    return
                row["season"] = max(1, s)
                row["episode"] = max(1, e)

            rows = self._load_overrides()
            rows = [x for x in rows if str(x.get("match") or "").strip().lower() != match.lower()]
            rows.insert(0, row)
            try:
                self._save_overrides(rows)
            except Exception as ex:
                self.status_var.set(f"Failed to save override: {ex}")
                return

            if reprocess_after_save:
                self.commands.put({"cmd": "reprocess_item", "file_id": int(fid)})
                self.status_var.set(f"Override saved; reprocessing item {fid}")
            else:
                self.status_var.set(f"Override saved for item {fid}")
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Save", command=on_save, style="TG.TButton").pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", command=win.destroy, style="TG.TButton").pack(side=tk.RIGHT, padx=(0, 8))

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

    def _is_visible_by_filter(self, fid: int) -> bool:
        mode = str(self.view_mode.get() or "all").lower()
        if mode != "failed":
            return True
        return str(self._raw_status_by_id.get(int(fid)) or "") == "Error"

    def _refresh_tree_filter(self):
        selected = self._selected_file_id()
        for fid, data in list(self._item_cache.items()):
            iid = str(fid)
            visible = self._is_visible_by_filter(fid)
            exists = iid in self._known_items
            if visible and not exists:
                self._known_items.add(iid)
                self.tree.insert("", tk.END, iid=iid, values=("", "", "", "", "", "", ""))
                self.tree.item(
                    iid,
                    values=(
                        str(data.get("queue_pos") or ""),
                        data.get("eta") or "--:--",
                        data.get("speed") or "0.00 MB/s",
                        f"{data.get('downloaded') or '0 B'} / {data.get('total') or '0 B'}",
                        f"{float(data.get('pct') or 0.0):5.1f}",
                        data.get("status") or "",
                        data.get("name") or "",
                    ),
                )
            if (not visible) and exists:
                self._known_items.discard(iid)
                try:
                    self.tree.delete(iid)
                except Exception:
                    pass
        if selected is not None and not self._is_visible_by_filter(selected):
            self._render_details(None)

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
                "classify_reason": ev.get("classify_reason") or "",
                "classify_confidence": ev.get("classify_confidence") or 0.0,
                "classify_dest_path": ev.get("classify_dest_path") or "",
                "classify_tmdb_used": bool(ev.get("classify_tmdb_used")),
                "classify_ai_used": bool(ev.get("classify_ai_used")),
                "classify_ai_confidence": ev.get("classify_ai_confidence") or 0.0,
            }
            self._raw_status_by_id[fid] = str(status_raw)

            if iid not in self._known_items and self._is_visible_by_filter(fid):
                self._known_items.add(iid)
                self.tree.insert("", tk.END, iid=iid, values=("", "", "", "", "", "", ""))
            if not self._is_visible_by_filter(fid):
                if iid in self._known_items:
                    self._known_items.discard(iid)
                    try:
                        self.tree.delete(iid)
                    except Exception:
                        pass
                if self._selected_file_id() == fid:
                    self._render_details(None)
                return

            status_cell = str(status)
            pct_cell = f"{pct:5.1f}" if isinstance(pct, (int, float)) else str(pct or "0.0")
            size_cell = f"{downloaded} / {total}"
            q_cell = str(qpos) if qpos else ""

            self.tree.item(iid, values=(q_cell, eta, speed, size_cell, pct_cell, status_cell, name))

            prev_raw = self._last_status_by_id.get(fid)
            self._last_status_by_id[fid] = str(status_raw)
            if prev_raw != status_raw:
                if status_raw == "Error" and self._notify_on_error:
                    self._notify("SaboniPlex Error", f"{name}")
                if status_raw == "Completed" and self._notify_on_complete:
                    self._notify("SaboniPlex Completed", f"{name}")

            sel = self._selected_file_id()
            if sel == fid:
                self._render_details(fid)
            self._refresh_review_queue()
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
                try:
                    self._raw_status_by_id.pop(int(fid), None)
                except Exception:
                    pass

            if removed_any:
                self._render_details(self._selected_file_id())
                self._refresh_review_queue()
            return

        if typ == "health":
            active = int(ev.get("active_downloads") or 0)
            queued = int(ev.get("queued_items") or 0)
            total = int(ev.get("total_items") or 0)
            chat_ids = ev.get("chat_ids") or []
            bootstrap = bool(ev.get("bootstrap_pending"))
            sync_state = "Syncing..." if bootstrap else "Live"
            self.health_var.set(f"Active {active} | Queue {queued} | Total {total} | Chats {len(chat_ids)} | {sync_state}")
            self._refresh_ai_status()
            return

        # ignore other events (log etc)

    def _on_close(self):
        self.commands.put({"cmd": "shutdown"})
        self.root.after(200, self.root.destroy)

    def _notify(self, title: str, message: str):
        if not self._notifications_enabled:
            return
        try:
            win = tk.Toplevel(self.root)
            win.title(title)
            win.geometry("320x110+40+40")
            win.attributes("-topmost", True)
            frm = ttk.Frame(win, padding=10)
            frm.pack(fill=tk.BOTH, expand=True)
            ttk.Label(frm, text=title).pack(anchor="w")
            ttk.Label(frm, text=message, wraplength=290).pack(anchor="w", pady=(6, 0))
            win.after(3000, win.destroy)
        except Exception:
            try:
                self.root.bell()
            except Exception:
                pass


def main():
    root = tk.Tk()
    SaboniPlexApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

"""
GUI for bulk-deploying WordPress to remote Ubuntu servers over SSH.

Paste tab-separated rows (e.g. copied straight from a spreadsheet), assign
which pasted column means what (domain, IP, ssh password, email, ...), fill
in SSH/WP defaults, then hit "Deploy All".

Run: python wp_deploy_gui.py   (requires: pip install paramiko)
"""

import csv
import datetime
import os
import queue
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

from wp_deploy_core import (
    append_credentials,
    find_theme_source,
    gen_password,
    locale_for_country,
    run_deploy,
)

ROLE_OPTIONS = [
    "Ignore",
    "Domain",
    "IP Address",
    "SSH Username",
    "SSH Password",
    "WP Admin Password",
    "WP Admin Email",
    "WP Site Title",
    "Country (locale)",
    "Note (kept in results only)",
]

DEFAULT_THEMES_DIR = r"C:\olegka\work\workingsites"


def app_dir() -> str:
    """Directory the program lives in (handles a frozen/exe build too)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

EXAMPLE = (
    "playbookopsin.com\twork\t\tiphoster regery\tGermany\tAlis Outlook 77\t"
    "91.199.32.243\t\tpXe208BOI5ziDV\t\tkravchenkoyuliya12@outlook.com\n"
    "proxyflowlab.com\twork\t\tmvps unstoppabledomains\tEngland\tDiana Farm Outlook 49\t"
    "178.157.82.244\t\tAO1B7xiWA5j4uhMN0bPSmZDl\tMvsQa8avsuCeqX\tartem.demchenko@outlook.com"
)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WP Remote Deploy")
        self.geometry("1150x780")

        self.rows_raw = []          # list[list[str]]
        self.role_vars = []         # list[tk.StringVar] per column
        self.row_status = {}        # row index -> StringVar for status
        self.log_queue = queue.Queue()
        self.results = []           # dicts for CSV export
        self.active_domains = set() # queued or running, to block double deploys

        self._build_paste_area()
        self._build_mapping_area()
        self._build_settings_area()
        self._build_table_area()
        self._build_log_area()

        self._install_clipboard_fixes()

        self.after(150, self._drain_log_queue)

    # ---------- UI sections ----------

    def _build_paste_area(self):
        frame = ttk.LabelFrame(self, text="1. Paste rows (tab-separated, from your spreadsheet)")
        frame.pack(fill="x", padx=8, pady=6)

        self.paste_text = tk.Text(frame, height=6)
        self.paste_text.pack(fill="x", padx=6, pady=6)
        self.paste_text.insert("1.0", EXAMPLE)

        btns = ttk.Frame(frame)
        btns.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btns, text="Paste from clipboard", command=self.paste_from_clipboard).pack(
            side="left"
        )
        ttk.Button(btns, text="Parse rows", command=self.parse_rows).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear", command=lambda: self.paste_text.delete("1.0", "end")).pack(
            side="left"
        )

    def _build_mapping_area(self):
        self.mapping_frame = ttk.LabelFrame(self, text="2. Assign column roles")
        self.mapping_frame.pack(fill="x", padx=8, pady=6)
        self.mapping_inner = ttk.Frame(self.mapping_frame)
        self.mapping_inner.pack(fill="x", padx=6, pady=6)

    def _build_settings_area(self):
        frame = ttk.LabelFrame(self, text="3. Defaults / SSH settings")
        frame.pack(fill="x", padx=8, pady=6)

        row1 = ttk.Frame(frame)
        row1.pack(fill="x", padx=6, pady=3)

        ttk.Label(row1, text="SSH port:").pack(side="left")
        self.ssh_port = tk.StringVar(value="22")
        ttk.Entry(row1, textvariable=self.ssh_port, width=6).pack(side="left", padx=(2, 12))

        ttk.Label(row1, text="Default SSH user:").pack(side="left")
        self.ssh_user = tk.StringVar(value="root")
        ttk.Entry(row1, textvariable=self.ssh_user, width=12).pack(side="left", padx=(2, 12))

        ttk.Label(row1, text="Auth method:").pack(side="left")
        self.auth_method = tk.StringVar(value="password")
        ttk.Combobox(
            row1, textvariable=self.auth_method, values=["password", "key file"],
            width=10, state="readonly"
        ).pack(side="left", padx=(2, 12))

        ttk.Label(row1, text="Key file (if key auth):").pack(side="left")
        self.key_path = tk.StringVar(value="")
        ttk.Entry(row1, textvariable=self.key_path, width=30).pack(side="left", padx=(2, 4))
        ttk.Button(row1, text="Browse...", command=self._browse_key).pack(side="left")

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", padx=6, pady=3)

        ttk.Label(row2, text="WP admin login: domain name (automatic)").pack(side="left")

        self.autogen_db_pw = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row2, text="Auto-generate DB password", variable=self.autogen_db_pw
        ).pack(side="left", padx=(12, 12))

        ttk.Label(row2, text="SSL: always requested via Let's Encrypt").pack(side="left")

        row2b = ttk.Frame(frame)
        row2b.pack(fill="x", padx=6, pady=3)

        ttk.Label(row2b, text="Themes folder (<domain>.zip or <domain>/ folder):").pack(side="left")
        self.themes_dir = tk.StringVar(value=DEFAULT_THEMES_DIR)
        ttk.Entry(row2b, textvariable=self.themes_dir, width=40).pack(side="left", padx=(2, 4))
        ttk.Button(row2b, text="Browse...", command=self._browse_themes_dir).pack(side="left")

        row3 = ttk.Frame(frame)
        row3.pack(fill="x", padx=6, pady=3)

        ttk.Label(row3, text="Concurrent deployments:").pack(side="left")
        self.concurrency = tk.StringVar(value="1")
        ttk.Spinbox(row3, from_=1, to=10, textvariable=self.concurrency, width=4).pack(
            side="left", padx=(2, 12)
        )

        ttk.Button(row3, text="Deploy All", command=self.deploy_all).pack(side="left", padx=6)
        ttk.Button(row3, text="Deploy Selected", command=self.deploy_selected).pack(
            side="left", padx=6
        )
        ttk.Button(row3, text="Export results CSV...", command=self.export_results).pack(
            side="left", padx=6
        )

    def _build_table_area(self):
        frame = ttk.LabelFrame(self, text="4. Parsed rows")
        frame.pack(fill="both", expand=True, padx=8, pady=6)

        self.tree = ttk.Treeview(frame, show="headings", height=8, selectmode="extended")
        self.tree.pack(fill="both", expand=True, side="left")

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

    def _build_log_area(self):
        frame = ttk.LabelFrame(self, text="Log")
        frame.pack(fill="both", expand=True, padx=8, pady=6)

        self.log_text = tk.Text(frame, height=14, state="disabled", bg="#111", fg="#ddd")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    # ---------- clipboard (works with non-Latin keyboard layouts) ----------

    def _install_clipboard_fixes(self):
        # On Windows, Ctrl+C/V/X/A bind to key *symbols*, which don't fire
        # when a non-Latin layout (e.g. Ukrainian/Russian) is active. Keycodes
        # are physical and layout-independent, so use those instead. Also add
        # a right-click context menu, since Tk gives Text/Entry none by default.
        self.bind_all("<Control-KeyPress>", self._handle_ctrl_key)
        self.bind_all("<Button-3>", self._show_context_menu)

    @staticmethod
    def _is_text_widget(widget):
        return isinstance(widget, (tk.Text, tk.Entry, ttk.Entry, ttk.Combobox))

    def _handle_ctrl_key(self, event):
        if not self._is_text_widget(event.widget):
            return None
        widget = event.widget
        keycode = event.keycode & 0xFF if event.keycode else 0
        if keycode == 0x56:  # V
            self._paste_into(widget)
            return "break"
        if keycode == 0x43:  # C
            self._copy_from(widget)
            return "break"
        if keycode == 0x58:  # X
            self._cut_from(widget)
            return "break"
        if keycode == 0x41:  # A
            self._select_all(widget)
            return "break"
        return None

    def _paste_into(self, widget):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        try:
            if isinstance(widget, tk.Text):
                widget.delete("sel.first", "sel.last")
            else:
                widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        widget.insert("insert", text)

    def _copy_from(self, widget):
        try:
            selected = widget.selection_get()
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(selected)

    def _cut_from(self, widget):
        try:
            selected = widget.selection_get()
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(selected)
        try:
            widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def _select_all(self, widget):
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end")
        elif isinstance(widget, (tk.Entry, ttk.Entry)):
            widget.selection_range(0, "end")

    def _show_context_menu(self, event):
        if not self._is_text_widget(event.widget):
            return
        widget = event.widget
        widget.focus_set()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Cut", command=lambda: self._cut_from(widget))
        menu.add_command(label="Copy", command=lambda: self._copy_from(widget))
        menu.add_command(label="Paste", command=lambda: self._paste_into(widget))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: self._select_all(widget))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------- actions ----------

    def paste_from_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("Clipboard empty", "Nothing text-based found on the clipboard.")
            return
        self.paste_text.delete("1.0", "end")
        self.paste_text.insert("1.0", text)
        self.log(f"Pasted {len(text.splitlines())} line(s) from clipboard.")

    def _browse_key(self):
        path = filedialog.askopenfilename(title="Select SSH private key")
        if path:
            self.key_path.set(path)

    def _browse_themes_dir(self):
        path = filedialog.askdirectory(title="Select themes folder")
        if path:
            self.themes_dir.set(path)

    def parse_rows(self):
        raw = self.paste_text.get("1.0", "end").strip("\n")
        lines = [ln for ln in raw.split("\n") if ln.strip()]
        self.rows_raw = [ln.split("\t") for ln in lines]

        if not self.rows_raw:
            messagebox.showwarning("No data", "Paste at least one row first.")
            return

        n_cols = max(len(r) for r in self.rows_raw)
        for r in self.rows_raw:
            while len(r) < n_cols:
                r.append("")

        self._build_column_mapping_widgets(n_cols)
        self._build_table_columns(n_cols)
        self.log(f"Parsed {len(self.rows_raw)} row(s), {n_cols} column(s).")

    def _build_column_mapping_widgets(self, n_cols):
        for w in self.mapping_inner.winfo_children():
            w.destroy()
        self.role_vars = []

        guesses = self._guess_roles(n_cols)
        for i in range(n_cols):
            col_frame = ttk.Frame(self.mapping_inner)
            col_frame.grid(row=0, column=i, padx=4, sticky="n")
            ttk.Label(col_frame, text=f"Col {i + 1}").pack()
            var = tk.StringVar(value=guesses[i])
            cb = ttk.Combobox(col_frame, textvariable=var, values=ROLE_OPTIONS, width=20, state="readonly")
            cb.pack()
            sample = self.rows_raw[0][i] if self.rows_raw and i < len(self.rows_raw[0]) else ""
            ttk.Label(col_frame, text=(sample[:18] + "...") if len(sample) > 18 else sample,
                      foreground="#666").pack()
            self.role_vars.append(var)

    def _guess_roles(self, n_cols):
        import re as _re
        ip_re = _re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
        guesses = ["Ignore"] * n_cols
        if self.rows_raw:
            sample_row = self.rows_raw[0]
            for i, val in enumerate(sample_row):
                v = val.strip()
                if i < n_cols:
                    if "." in v and " " not in v and "@" not in v and not ip_re.match(v) and i == 0:
                        guesses[i] = "Domain"
                    elif ip_re.match(v):
                        guesses[i] = "IP Address"
                    elif "@" in v:
                        guesses[i] = "WP Admin Email"
        return guesses

    def _build_table_columns(self, n_cols):
        cols = [f"c{i}" for i in range(n_cols)] + ["status"]
        self.tree.configure(columns=cols)
        for i in range(n_cols):
            self.tree.heading(f"c{i}", text=f"Col {i + 1}")
            self.tree.column(f"c{i}", width=110, anchor="w")
        self.tree.heading("status", text="Status")
        self.tree.column("status", width=140, anchor="w")

        self.tree.delete(*self.tree.get_children())
        for idx, row in enumerate(self.rows_raw):
            self.tree.insert("", "end", iid=str(idx), values=row + ["pending"])

    def _row_values_by_role(self, row_idx):
        row = self.rows_raw[row_idx]
        values = {}
        for i, var in enumerate(self.role_vars):
            role = var.get()
            if role == "Ignore":
                continue
            values.setdefault(role, row[i])
        return values

    def _build_job(self, row_idx):
        vals = self._row_values_by_role(row_idx)
        domain = vals.get("Domain", "").strip()
        ip = vals.get("IP Address", "").strip()
        if not domain or not ip:
            raise ValueError("Domain and IP Address must be mapped and non-empty for every row.")

        ssh_user = vals.get("SSH Username", "").strip() or self.ssh_user.get().strip() or "root"
        ssh_password = vals.get("SSH Password", "").strip()

        # WP admin login is always the site's domain name.
        wp_admin_user = domain
        wp_admin_password = vals.get("WP Admin Password", "").strip() or gen_password(16)

        wp_admin_email = vals.get("WP Admin Email", "").strip()
        if not wp_admin_email:
            raise ValueError(f"[{domain}] WP Admin Email is required.")

        wp_title = vals.get("WP Site Title", "").strip() or domain
        country = vals.get("Country (locale)", "").strip()
        note = vals.get("Note (kept in results only)", "").strip()

        db_password = gen_password(20) if self.autogen_db_pw.get() else (ssh_password or gen_password(20))

        theme_zip_path, theme_zip_is_temp = find_theme_source(
            self.themes_dir.get().strip(), domain
        )

        job = {
            "domain": domain,
            "ip": ip,
            "ssh_user": ssh_user,
            "ssh_password": ssh_password,
            "wp_admin_user": wp_admin_user,
            "wp_admin_password": wp_admin_password,
            "wp_admin_email": wp_admin_email,
            "wp_title": wp_title,
            "locale": locale_for_country(country),
            "db_password": db_password,
            "theme_zip_path": theme_zip_path,
            "theme_zip_is_temp": theme_zip_is_temp,
            "note": note,
        }
        return job

    def deploy_all(self):
        self._deploy(list(range(len(self.rows_raw))))

    def deploy_selected(self):
        sel = [int(i) for i in self.tree.selection()]
        if not sel:
            messagebox.showinfo("Nothing selected", "Select one or more rows in the table first.")
            return
        self._deploy(sel)

    def _deploy(self, row_indices):
        if not self.rows_raw:
            messagebox.showwarning("No data", "Parse rows first.")
            return

        auth_method = self.auth_method.get()
        if auth_method == "password":
            auth_template = {"method": "password"}
        else:
            if not self.key_path.get().strip():
                messagebox.showerror("Missing key", "Select an SSH private key file.")
                return
            auth_template = {"method": "key", "key_path": self.key_path.get().strip()}

        try:
            port = int(self.ssh_port.get())
        except ValueError:
            messagebox.showerror("Invalid port", "SSH port must be a number.")
            return

        jobs = []
        for idx in row_indices:
            # A second run on the same site would fight the first for the dpkg
            # lock and overwrite its freshly generated passwords.
            domain = self._row_values_by_role(idx).get("Domain", "").strip()
            if domain in self.active_domains:
                self.log(f"SKIPPED row {idx + 1}: {domain} is already being deployed.")
                continue
            try:
                job = self._build_job(idx)
            except ValueError as e:
                self.log(f"SKIPPED row {idx + 1}: {e}")
                self._set_status(idx, "skipped (bad mapping)")
                continue
            auth = dict(auth_template)
            if auth["method"] == "password":
                auth["password"] = job["ssh_password"]
                if not auth["password"]:
                    self.log(f"SKIPPED row {idx + 1}: no SSH password mapped and auth method is 'password'.")
                    self._set_status(idx, "skipped (no ssh pw)")
                    continue
            if not job["theme_zip_path"]:
                self.log(f"[{job['domain']}] No theme found "
                         f"({job['domain']}.zip or {job['domain']}/ folder) "
                         f"in themes folder — deploying with the default WP theme.")
            jobs.append((idx, job, auth, port))
            self.active_domains.add(job["domain"])

        if not jobs:
            return

        try:
            max_workers = max(1, int(self.concurrency.get()))
        except ValueError:
            max_workers = 2

        for idx, job, _, _ in jobs:
            self._set_status(idx, "queued")

        threading.Thread(target=self._run_jobs, args=(jobs, max_workers), daemon=True).start()

    def _run_jobs(self, jobs, max_workers):
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for idx, job, auth, port in jobs:
                self._set_status(idx, "running")
                fut = pool.submit(self._run_one, idx, job, auth, port)
                futures[fut] = (idx, job)

            for fut in futures:
                idx, job = futures[fut]
                try:
                    ok = fut.result()
                    self._set_status(idx, "done" if ok else "error")
                    self.results.append({**job, "success": ok})
                    if ok:
                        creds_path = os.path.join(app_dir(), "credentials.txt")
                        try:
                            append_credentials(creds_path, job["domain"], job["wp_admin_password"])
                        except OSError as e:
                            self.log_queue.put(
                                f"[{job['domain']}] WARNING: could not write credentials.txt: {e}"
                            )
                except Exception as e:
                    self.log_queue.put(f"[{job['domain']}] ERROR: {e}")
                    self._set_status(idx, "error")
                    self.results.append({**job, "success": False, "error": str(e)})

    def _run_one(self, idx, job, auth, port):
        def cb(line):
            self.log_queue.put(line)
        try:
            return run_deploy(job["ip"], port, job["ssh_user"], auth, job, cb, diag_dir=app_dir())
        finally:
            self.active_domains.discard(job["domain"])

    def _set_status(self, idx, text):
        def apply():
            if self.tree.exists(str(idx)):
                vals = list(self.tree.item(str(idx), "values"))
                vals[-1] = text
                self.tree.item(str(idx), values=vals)
        self.after(0, apply)

    def export_results(self):
        if not self.results:
            messagebox.showinfo("No results", "Nothing deployed yet.")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"deploy_results_{ts}.csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default_name,
            filetypes=[("CSV", "*.csv")]
        )
        if not path:
            return
        fieldnames = ["domain", "ip", "ssh_user", "wp_admin_user", "wp_admin_password",
                      "wp_admin_email", "db_password", "locale", "note", "success", "error"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in self.results:
                writer.writerow(r)
        self.log(f"Results exported to {path} (contains plaintext passwords — keep it safe).")

    def log(self, msg):
        self.log_queue.put(msg)

    def _drain_log_queue(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(150, self._drain_log_queue)


if __name__ == "__main__":
    App().mainloop()

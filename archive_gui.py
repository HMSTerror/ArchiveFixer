"""Tkinter GUI for archive renaming and extraction with installed tools."""

from __future__ import annotations

from pathlib import Path
from queue import Empty, Queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from archive_extractors import EXTRACTOR_LABELS, find_extractor
from archive_engine import (
    CleanupError,
    ExtractionEngine,
    ExtractionError,
    ExtractionJob,
    discover_archive_inputs,
    expand_dropped_folder,
    flatten_existing_result,
    is_linked_path,
)
from archive_logic import extraction_base, plan_renames
from password_store import default_store_path, load_password_presets, save_password_presets
from version import VERSION


DEFAULT_PASSWORD = ""
BACKEND_BY_LABEL = {label: kind for kind, label in EXTRACTOR_LABELS.items()}


def find_7zip() -> Path | None:
    return find_extractor("7zip")


class ArchiveApp:
    def __init__(self, root: tk.Tk, preset_path: Path | None = None) -> None:
        self.root = root
        self.root.title(f"ArchiveFixer v{VERSION} — 压缩包后缀修复与解压")
        self.root.geometry("1120x760")
        self.root.minsize(800, 540)
        self.rows: dict[str, Path] = {}
        self.folder_archives: dict[str, tuple[Path, ...]] = {}
        self.events: Queue[tuple[str, str]] = Queue()
        self.busy = False
        self.preset_path = preset_path or default_store_path()

        self.extractor_paths = {kind: str(find_extractor(kind) or "") for kind in EXTRACTOR_LABELS}
        self.active_backend = next((kind for kind, path in self.extractor_paths.items() if path), "7zip")
        self.extractor_choice = tk.StringVar(value=EXTRACTOR_LABELS[self.active_backend])
        self.extractor_path = tk.StringVar(value=self.extractor_paths[self.active_backend])
        self.rename_suffix = tk.StringVar(value=".7z")
        self.password_vars = [tk.StringVar(value=value) for value in load_password_presets(self.preset_path, DEFAULT_PASSWORD)]
        self.password = self.password_vars[0]
        self.output_dir = tk.StringVar()
        self.recursive = tk.BooleanVar(value=True)
        self.concurrent = tk.IntVar(value=2)
        self.delete_archives = tk.BooleanVar(value=False)
        self.show_password = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="拖入文件或文件夹后，先预览再执行。")

        self._build_ui()
        self._event_poller = self.root.after(100, self._process_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        title = ttk.Label(outer, text="压缩包后缀修复与解压", font=("Microsoft YaHei UI", 17, "bold"))
        title.grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(
            outer,
            text="普通文件：按所选后缀改名（仅改文件名，不转换压缩格式）；分卷文件：保留 .001 / .002 等编号，删除其后的伪装后缀。",
            wraplength=1000,
        ).grid(row=1, column=0, sticky="ew", pady=(0, 10))

        files_frame = ttk.LabelFrame(outer, text="文件与改名预览", padding=8)
        files_frame.grid(row=2, column=0, sticky="nsew")
        files_frame.columnconfigure(0, weight=1)
        files_frame.rowconfigure(2, weight=1)

        buttons = ttk.Frame(files_frame)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.file_buttons = []
        add_files_button = ttk.Button(buttons, text="添加文件", command=self._add_files)
        add_files_button.pack(side="left", padx=(0, 6))
        self.file_buttons.append(add_files_button)
        add_folder_button = ttk.Button(buttons, text="添加文件夹", command=self._add_folder)
        add_folder_button.pack(side="left", padx=(0, 6))
        self.file_buttons.append(add_folder_button)
        ttk.Label(buttons, text="改名后缀").pack(side="left", padx=(8, 4))
        suffix_picker = ttk.Combobox(buttons, textvariable=self.rename_suffix, values=(".7z", ".zip"), state="readonly", width=6)
        suffix_picker.pack(side="left", padx=(0, 12))
        suffix_picker.bind("<<ComboboxSelected>>", lambda event: self._refresh_preview())
        ttk.Checkbutton(buttons, text="包含子文件夹", variable=self.recursive).pack(side="left", padx=(0, 15))
        for label, command in (("全选", self._select_all), ("移除所选", self._remove_selected), ("清空列表", self._clear)):
            button = ttk.Button(buttons, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.file_buttons.append(button)

        drop_label = ttk.Label(
            files_frame,
            text="将多个文件或文件夹拖入此处；混合父目录会拆成独立压缩包与文件夹任务",
            anchor="center",
            relief="groove",
            padding=6,
        )
        drop_label.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        table_frame = ttk.Frame(files_frame)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(table_frame, columns=("source", "target", "state"), show="headings", selectmode="extended", height=6)
        self.table.heading("source", text="原始路径")
        self.table.heading("target", text="改名 / 结果位置")
        self.table.heading("state", text="状态")
        self.table.column("source", width=570, minwidth=230)
        self.table.column("target", width=260, minwidth=140)
        self.table.column("state", width=180, minwidth=110)
        self.table.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=yscroll.set)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.table.configure(xscrollcommand=xscroll.set)
        for widget in (drop_label, self.table):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)

        settings = ttk.LabelFrame(outer, text="解压设置", padding=8)
        settings.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="解压程序").grid(row=0, column=0, sticky="w")
        extractor_row = ttk.Frame(settings)
        extractor_row.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
        extractor_row.columnconfigure(1, weight=1)
        extractor_picker = ttk.Combobox(
            extractor_row, textvariable=self.extractor_choice,
            values=tuple(EXTRACTOR_LABELS.values()), state="readonly", width=12,
        )
        extractor_picker.grid(row=0, column=0, padx=(0, 8))
        extractor_picker.bind("<<ComboboxSelected>>", self._on_extractor_change)
        ttk.Entry(extractor_row, textvariable=self.extractor_path).grid(row=0, column=1, sticky="ew")
        ttk.Button(settings, text="浏览", command=self._browse_extractor).grid(row=0, column=2)
        ttk.Label(settings, text="密码预设（依次尝试）").grid(row=1, column=0, sticky="w")
        password_panel = ttk.Frame(settings)
        password_panel.grid(row=1, column=1, columnspan=2, sticky="w", padx=8, pady=3)
        self.password_entries = []
        for index, variable in enumerate(self.password_vars, start=1):
            cell = ttk.Frame(password_panel)
            cell.pack(side="left", padx=(0, 7))
            ttk.Label(cell, text=f"密码 {index}").pack(anchor="w")
            entry = ttk.Entry(cell, textvariable=variable, show="●", width=14)
            entry.pack()
            self.password_entries.append(entry)
        ttk.Checkbutton(password_panel, text="显示", variable=self.show_password, command=self._toggle_password).pack(side="left", padx=(2, 0))
        ttk.Button(password_panel, text="保存预设", command=self._save_passwords).pack(side="left", padx=(8, 0))
        ttk.Label(settings, text="解压目标目录").grid(row=2, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.output_dir).grid(row=2, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(settings, text="浏览", command=self._browse_output).grid(row=2, column=2)
        ttk.Label(settings, text="留空则在每个压缩包旁生成一个“文件名_解压”结果目录，去掉压缩层目录。", foreground="#555555").grid(row=3, column=1, sticky="w", padx=8)
        ttk.Checkbutton(
            settings,
            text="全部层级成功后删除原始及中间压缩包（默认关闭）",
            variable=self.delete_archives,
        ).grid(row=4, column=1, columnspan=2, sticky="w", padx=8, pady=(5, 0))
        ttk.Label(settings, text="同时解压任务数").grid(row=5, column=0, sticky="w", pady=(5, 0))
        ttk.Spinbox(settings, from_=1, to=8, textvariable=self.concurrent, width=5).grid(
            row=5, column=1, sticky="w", padx=8, pady=(5, 0)
        )
        ttk.Label(settings, text="多个外层压缩包并行；同一个压缩包的内层依次处理。", foreground="#555555").grid(
            row=5, column=1, sticky="w", padx=(70, 0), pady=(5, 0)
        )

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.action_buttons = []
        for label, command in (
            ("改名所选", self._rename_selected),
            ("逐层解压所选", self._extract_selected),
            ("改名并逐层解压所选", self._rename_and_extract),
            ("整理已有结果", self._flatten_existing),
        ):
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side="left", padx=(0, 8))
            self.action_buttons.append(button)
        ttk.Label(actions, textvariable=self.status).pack(side="left", padx=8)

        log_frame = ttk.LabelFrame(outer, text="操作记录", padding=8)
        log_frame.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.log = tk.Text(log_frame, height=4, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择需要处理的文件")
        self._add_paths([Path(path) for path in paths])

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择包含压缩包的文件夹")
        if selected:
            self._add_input_paths([Path(selected)])

    def _on_drop(self, event: tk.Event) -> str:
        if self.busy:
            self._log("解压期间暂不添加文件；请等待当前批次完成。")
            return "break"
        try:
            paths = [Path(path) for path in self.root.tk.splitlist(event.data)]
        except tk.TclError as exc:
            messagebox.showerror("拖入失败", str(exc))
            return "break"
        self._add_input_paths(paths)
        return "copy"

    def _add_input_paths(self, paths: list[Path]) -> None:
        if self.busy:
            return
        collected = []
        try:
            for path in paths:
                if path.is_dir() and not is_linked_path(path):
                    collected.extend(expand_dropped_folder(path, self.recursive.get()))
                elif path.is_file() and not is_linked_path(path):
                    collected.append(path)
        except ExtractionError as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if not collected:
            self.status.set("未找到可识别的压缩包；可直接添加单个文件。")
            return
        self._add_paths(collected)

    def _add_paths(self, paths: list[Path]) -> None:
        if self.busy:
            return
        existing = {str(path).casefold() for path in self.rows.values()}
        added = []
        for path in paths:
            if is_linked_path(path) or not (path.is_file() or path.is_dir()) or str(path).casefold() in existing:
                continue
            if path.is_dir():
                try:
                    folder_archives = tuple(discover_archive_inputs(path, self.recursive.get()))
                except ExtractionError as exc:
                    self._log(f"跳过无法扫描的文件夹：{path}：{exc}")
                    continue
            else:
                folder_archives = ()
            iid = self.table.insert("", "end")
            self.rows[iid] = path
            if path.is_dir():
                self.folder_archives[iid] = folder_archives
            existing.add(str(path).casefold())
            added.append(iid)
        self._refresh_preview()
        if added:
            self.table.selection_set(added)
            self.status.set(f"已添加 {len(added)} 个任务；列表共 {len(self.rows)} 个。")

    def _refresh_preview(self) -> None:
        file_iids = [iid for iid in self.rows if iid not in self.folder_archives]
        decisions = dict(zip(file_iids, plan_renames([self.rows[iid] for iid in file_iids], self.rename_suffix.get())))
        for iid, path in self.rows.items():
            if iid in self.folder_archives:
                archives = self.folder_archives[iid]
                decisions_in_folder = plan_renames(list(archives), self.rename_suffix.get())
                pending = sum(decision.status == "rename" for decision in decisions_in_folder)
                conflicts = sum(decision.status == "conflict" for decision in decisions_in_folder)
                state = (
                    f"文件夹任务：{len(archives)} 个压缩文件，待改名 {pending}，冲突 {conflicts}"
                    if path.is_dir() else "源文件夹不存在"
                )
                self.table.item(iid, values=(str(path), f"内部改为 {self.rename_suffix.get()}；结果留原文件夹", state))
            else:
                decision = decisions[iid]
                state = decision.detail if decision.source.exists() else "源文件不存在"
                self.table.item(iid, values=(str(decision.source), decision.target.name, state))

    def _selected(self) -> list[str]:
        selection = list(self.table.selection())
        if not selection:
            messagebox.showinfo("未选择文件", "请在列表中选择要处理的文件；可点击“全选”。")
        return selection

    def _select_all(self) -> None:
        self.table.selection_set(list(self.rows))

    def _remove_selected(self) -> None:
        for iid in self.table.selection():
            self.table.delete(iid)
            self.rows.pop(iid, None)
            self.folder_archives.pop(iid, None)
        self._refresh_preview()

    def _clear(self) -> None:
        self.table.delete(*self.table.get_children())
        self.rows.clear()
        self.folder_archives.clear()
        self.status.set("列表已清空。")

    def _on_extractor_change(self, event: tk.Event | None = None) -> None:
        self.extractor_paths[self.active_backend] = self.extractor_path.get()
        self.active_backend = BACKEND_BY_LABEL[self.extractor_choice.get()]
        self.extractor_path.set(self.extractor_paths[self.active_backend])

    def _browse_extractor(self) -> None:
        label = self.extractor_choice.get()
        selected = filedialog.askopenfilename(
            title=f"选择 {label} 的命令行程序",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if selected:
            self.extractor_path.set(selected)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(title="选择解压目标目录")
        if selected:
            self.output_dir.set(selected)

    def _toggle_password(self) -> None:
        for entry in self.password_entries:
            entry.configure(show="" if self.show_password.get() else "●")

    def _save_passwords(self) -> None:
        try:
            save_password_presets(self.preset_path, [variable.get() for variable in self.password_vars])
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.status.set("5 个密码预设已保存，下次打开时会自动载入。")

    def _rename_folder_archives(self, iid: str) -> tuple[int, bool]:
        folder = self.rows[iid]
        try:
            paths = discover_archive_inputs(folder, self.recursive.get())
        except ExtractionError as exc:
            self._log(f"文件夹扫描失败：{folder}：{exc}")
            return 0, False
        if not paths:
            self._log(f"文件夹中没有可处理的压缩包：{folder}")
            return 0, False
        decisions = plan_renames(paths, self.rename_suffix.get())
        blocked = [decision for decision in decisions if decision.status == "conflict" or not decision.source.exists()]
        if blocked:
            for decision in blocked:
                self._log(f"文件夹改名已跳过：{decision.source} → {decision.target.name}：{decision.detail}")
            return 0, False

        applied = []
        try:
            for decision in decisions:
                if decision.status != "rename":
                    continue
                if decision.target.exists():
                    raise FileExistsError(f"目标文件已存在：{decision.target}")
                decision.source.rename(decision.target)
                applied.append(decision)
        except OSError as exc:
            rollback_errors = []
            for decision in reversed(applied):
                try:
                    decision.target.rename(decision.source)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{decision.target}：{rollback_exc}")
            self._log(f"文件夹改名失败：{folder}：{exc}")
            if rollback_errors:
                self._log(f"部分文件无法恢复原名：{'；'.join(rollback_errors)}")
            return 0, False

        self.folder_archives[iid] = tuple(decision.target for decision in decisions)
        changed = {decision.source: decision.target for decision in applied}
        for row_id, path in self.rows.items():
            if row_id != iid and path in changed:
                self.rows[row_id] = changed[path]
        for decision in applied:
            self._log(f"已改名：{decision.source.name} → {decision.target.name}")
        return len(applied), True

    def _rename(self, selected: list[str]) -> tuple[list[str], int]:
        if not self._validate_folder_selection(selected):
            return [], len(selected)
        selected_folders = [self.rows[iid] for iid in selected if iid in self.folder_archives]
        file_iids = [iid for iid in selected if iid not in self.folder_archives and not any(
            self.rows[iid].is_relative_to(folder) for folder in selected_folders
        )]
        decisions = dict(zip(file_iids, plan_renames([self.rows[iid] for iid in file_iids], self.rename_suffix.get())))
        ready = []
        renamed = skipped = 0
        for iid in selected:
            if iid in self.folder_archives:
                changed, success = self._rename_folder_archives(iid)
                renamed += changed
                if success:
                    ready.append(iid)
                else:
                    skipped += 1
                continue
            if any(self.rows[iid].is_relative_to(folder) for folder in selected_folders):
                self._log(f"文件已由所选文件夹任务包含，避免重复改名：{self.rows[iid]}")
                ready.append(iid)
                continue
            decision = decisions[iid]
            if not decision.source.exists():
                self._log(f"跳过：源文件不存在：{decision.source}")
                skipped += 1
            elif decision.status == "conflict":
                self._log(f"跳过：{decision.detail}：{decision.source} → {decision.target.name}")
                skipped += 1
            elif decision.status == "unchanged":
                ready.append(iid)
            else:
                try:
                    if decision.target.exists():
                        raise FileExistsError("目标文件已存在")
                    decision.source.rename(decision.target)
                except OSError as exc:
                    self._log(f"改名失败：{decision.source}：{exc}")
                    skipped += 1
                else:
                    self.rows[iid] = decision.target
                    self._log(f"已改名：{decision.source.name} → {decision.target.name}")
                    ready.append(iid)
                    renamed += 1
        self._refresh_preview()
        self.status.set(f"改名完成：成功 {renamed}，跳过/失败 {skipped}。")
        return ready, skipped

    def _rename_selected(self) -> None:
        if self.busy:
            return
        selected = self._selected()
        if selected:
            self._rename(selected)

    def _extract_selected(self) -> None:
        if self.busy:
            return
        selected = self._selected()
        if selected:
            self._start_extract(selected)

    def _rename_and_extract(self) -> None:
        if self.busy:
            return
        selected = self._selected()
        if selected:
            if not self._preflight_combined(selected):
                return
            ready, skipped = self._rename(selected)
            if skipped:
                self._log("部分文件改名失败，本批次未开始解压；请处理后重试。")
            elif ready:
                self._start_extract(ready)

    def _flatten_existing(self) -> None:
        if self.busy:
            return
        selected = filedialog.askdirectory(title="选择需要整理的最外层“文件名_解压”文件夹")
        if not selected:
            return
        try:
            result, removed = flatten_existing_result(Path(selected))
        except (ExtractionError, CleanupError) as exc:
            self._log(f"整理已有结果失败：{exc}")
            messagebox.showerror("整理失败", str(exc))
        else:
            message = f"已去除 {removed} 层外壳，最终内容位于：{result}"
            self._log(message)
            self.status.set(message)

    def _resolve_extractor(self) -> tuple[Path, str] | None:
        label = self.extractor_choice.get()
        backend = BACKEND_BY_LABEL.get(label)
        if backend is None:
            messagebox.showerror("解压程序无效", f"不支持的解压程序：{label}")
            return None
        executable = Path(self.extractor_path.get().strip().strip('"'))
        if not executable.is_file():
            names = {"7zip": "7z.exe", "winrar": "WinRAR.exe", "bandizip": "bz.exe"}
            messagebox.showerror("找不到解压程序", f"请安装 {label}，并选择本机的 {names[backend]}。")
            return None
        expected = {
            "7zip": {"7z.exe", "7za.exe"},
            "winrar": {"winrar.exe"},
            "bandizip": {"bz.exe", "bz.x64.exe", "bz.x86.exe", "bz.a64.exe"},
        }
        if executable.name.casefold() not in expected[backend]:
            messagebox.showerror("程序类型不匹配", f"请选择 {label} 的命令行程序。")
            return None
        return executable, backend

    def _validate_folder_selection(self, selected: list[str]) -> bool:
        folders = [self.rows[iid].resolve() for iid in selected if iid in self.folder_archives]
        for index, folder in enumerate(folders):
            for other in folders[index + 1:]:
                if folder.is_relative_to(other) or other.is_relative_to(folder):
                    messagebox.showerror("重复选择", f"同一批请只保留父文件夹或子文件夹中的一个：\n{folder}\n{other}")
                    return False
        return True

    def _preflight_combined(self, selected: list[str]) -> bool:
        if self._resolve_extractor() is None:
            return False
        if not self._validate_folder_selection(selected):
            return False
        try:
            max_concurrent = self.concurrent.get()
        except tk.TclError:
            max_concurrent = 0
        if not 1 <= max_concurrent <= 8:
            messagebox.showerror("任务数无效", "同时解压任务数请输入 1 到 8。")
            return False
        output_text = self.output_dir.get().strip()
        output_root = Path(output_text).expanduser() if output_text else None
        if output_root is not None and output_root.exists() and not output_root.is_dir():
            messagebox.showerror("目标目录无效", f"解压目标不是文件夹：{output_root}")
            return False
        selected_folders = [self.rows[iid] for iid in selected if iid in self.folder_archives]
        file_iids = [iid for iid in selected if iid not in self.folder_archives and not any(
            self.rows[iid].is_relative_to(folder) for folder in selected_folders
        )]
        decisions = dict(zip(file_iids, plan_renames([self.rows[iid] for iid in file_iids], self.rename_suffix.get())))
        for iid in file_iids:
            decision = decisions[iid]
            candidate = decision.target if decision.status == "rename" else decision.source
            base = extraction_base(candidate)
            if base is None:
                continue
            destination = (output_root or candidate.parent) / f"{base}_解压"
            if destination.exists() or is_linked_path(destination):
                messagebox.showerror("目标目录已存在", f"避免覆盖，解压前请处理已有目录：{destination}")
                return False
        return True

    def _start_extract(self, selected: list[str]) -> None:
        if not self._validate_folder_selection(selected):
            return
        resolved = self._resolve_extractor()
        if resolved is None:
            return
        executable, backend = resolved
        output_text = self.output_dir.get().strip()
        output_root = Path(output_text).expanduser() if output_text else None
        try:
            max_concurrent = self.concurrent.get()
        except tk.TclError:
            max_concurrent = 0
        if not 1 <= max_concurrent <= 8:
            messagebox.showerror("任务数无效", "同时解压任务数请输入 1 到 8。")
            return
        jobs = []
        selected_folders = [self.rows[iid] for iid in selected if iid in self.folder_archives]
        for iid in selected:
            path = self.rows[iid]
            if iid in self.folder_archives:
                try:
                    archives = tuple(discover_archive_inputs(path, self.recursive.get()))
                except ExtractionError as exc:
                    messagebox.showerror("文件夹扫描失败", str(exc))
                    return
                if not archives:
                    messagebox.showerror("没有压缩包", f"文件夹中未找到可识别的压缩包：{path}")
                    return
                self.folder_archives[iid] = archives
                jobs.append(ExtractionJob(path, path.name, folder_mode=True, archives=archives))
                continue
            if any(path.is_relative_to(folder) for folder in selected_folders):
                self._log(f"文件已由所选文件夹任务包含，避免重复解压：{path}")
                continue
            base = extraction_base(path)
            if base is None:
                self._log(f"跳过解压：请先改名，或当前文件不是可识别的压缩包首卷：{path.name}")
                continue
            jobs.append(ExtractionJob(path, base))
        if not jobs:
            self.status.set("没有可解压的压缩包；分卷只从 .001 首卷解压。")
            return
        self.busy = True
        for button in self.action_buttons + self.file_buttons:
            button.configure(state="disabled")
        self.status.set(f"正在处理 {len(jobs)} 个任务，同时最多 {max_concurrent} 个…")
        passwords = tuple(variable.get() for variable in self.password_vars)
        thread = threading.Thread(
            target=self._extract_worker,
            args=(executable, backend, jobs, output_root, passwords, self.delete_archives.get(), max_concurrent),
            daemon=True,
        )
        thread.start()

    def _extract_worker(
        self,
        executable: Path,
        backend: str,
        jobs: list[ExtractionJob],
        output_root: Path | None,
        passwords: tuple[str, ...],
        delete_archives: bool,
        max_concurrent: int,
    ) -> None:
        engine = ExtractionEngine(executable, passwords, lambda message: self.events.put(("log", message)), backend)
        try:
            result = engine.run(jobs, output_root, delete_archives, max_concurrent)
        except ExtractionError as exc:
            self.events.put(("log", f"解压未全部完成，未删除压缩包：{exc}"))
            self.events.put(("done", "解压失败；压缩包已保留，已解出的文件可供检查。"))
        except CleanupError as exc:
            self.events.put(("log", f"解压完成，但清理未完成：{exc}"))
            self.events.put(("done", "解压完成；部分压缩包清理失败，请查看操作记录。"))
        except Exception as exc:
            self.events.put(("log", f"处理意外中断：{exc}"))
            self.events.put(("done", "处理意外中断；请查看操作记录。"))
        else:
            for error in result.errors:
                self.events.put(("log", f"任务失败：{error}"))
            for warning in result.warnings:
                self.events.put(("log", f"清理提示：{warning}"))
            summary = (
                f"完成：成功 {result.successful_jobs}/{result.outer_archives} 个任务，"
                f"失败 {result.failed_jobs} 个，清理提示 {len(result.warnings)} 个，累计 {result.layers} 层，"
                f"最终文件 {result.final_files} 个，删除压缩包文件 {result.deleted_archives} 个。"
            )
            self.events.put(("log", summary))
            self.events.put(("done", summary))

    def _close(self) -> None:
        if self.busy:
            messagebox.showinfo("正在解压", "请等待本批次解压及清理完成后再关闭窗口。")
            return
        self.root.after_cancel(self._event_poller)
        self.root.destroy()

    def _process_events(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "log":
                    self._log(message)
                elif kind == "done":
                    self.busy = False
                    for button in self.action_buttons + self.file_buttons:
                        button.configure(state="normal")
                    self._refresh_preview()
                    self.status.set(message)
        except Empty:
            pass
        self._event_poller = self.root.after(100, self._process_events)


def main() -> None:
    root = TkinterDnD.Tk()
    ArchiveApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

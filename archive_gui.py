"""Tkinter GUI for previewing archive renames and extracting with local 7-Zip."""

from __future__ import annotations

import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from archive_engine import (
    CleanupError,
    ExtractionEngine,
    ExtractionError,
    ExtractionJob,
    discover_archive_inputs,
    expand_dropped_folder,
    flatten_existing_result,
)
from archive_logic import extraction_base, plan_renames
from password_store import default_store_path, load_password_presets, save_password_presets


DEFAULT_PASSWORD = ""


def find_7zip() -> Path | None:
    candidates = [shutil.which("7z"), shutil.which("7za")]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if root:
            candidates.append(str(Path(root) / "7-Zip" / "7z.exe"))
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key_name in (r"SOFTWARE\7-Zip", r"SOFTWARE\WOW6432Node\7-Zip"):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        install_dir, _ = winreg.QueryValueEx(key, "Path")
                        candidates.append(str(Path(install_dir) / "7z.exe"))
                except OSError:
                    pass
    except ImportError:
        pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


class ArchiveApp:
    def __init__(self, root: tk.Tk, preset_path: Path | None = None) -> None:
        self.root = root
        self.root.title("压缩包后缀修复与解压")
        self.root.geometry("1120x760")
        self.root.minsize(800, 540)
        self.rows: dict[str, Path] = {}
        self.folder_archives: dict[str, tuple[Path, ...]] = {}
        self.events: Queue[tuple[str, str]] = Queue()
        self.busy = False
        self.preset_path = preset_path or default_store_path()

        self.seven_zip = tk.StringVar(value=str(find_7zip() or ""))
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
            text="普通文件：改为 .7z（已有 .7z 时去掉后续伪装）；分卷文件：保留 .001 / .002 等三位编号，删除其后的伪装后缀。",
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
        ttk.Label(settings, text="7-Zip 程序").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.seven_zip).grid(row=0, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(settings, text="浏览", command=self._browse_7zip).grid(row=0, column=2)
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
                if path.is_dir() and not path.is_symlink():
                    collected.extend(expand_dropped_folder(path, self.recursive.get()))
                elif path.is_file() and not path.is_symlink():
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
            if path.is_symlink() or not (path.is_file() or path.is_dir()) or str(path).casefold() in existing:
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
        decisions = dict(zip(file_iids, plan_renames([self.rows[iid] for iid in file_iids])))
        for iid, path in self.rows.items():
            if iid in self.folder_archives:
                count = len(self.folder_archives[iid])
                state = f"文件夹任务：{count} 个候选压缩文件" if path.is_dir() else "源文件夹不存在"
                self.table.item(iid, values=(str(path), "直接放回此文件夹", state))
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

    def _browse_7zip(self) -> None:
        selected = filedialog.askopenfilename(title="选择 7z.exe", filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if selected:
            self.seven_zip.set(selected)

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

    def _rename(self, selected: list[str]) -> tuple[list[str], int]:
        file_iids = [iid for iid in self.rows if iid not in self.folder_archives]
        decisions = dict(zip(file_iids, plan_renames([self.rows[iid] for iid in file_iids])))
        ready = []
        renamed = skipped = 0
        for iid in selected:
            if iid in self.folder_archives:
                self._log(f"文件夹任务保留原名；内部伪装分卷会在解压时处理：{self.rows[iid]}")
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

    def _resolve_7zip(self) -> Path | None:
        executable = Path(self.seven_zip.get().strip().strip('"'))
        if not executable.is_file():
            messagebox.showerror("找不到 7-Zip", "请在“7-Zip 程序”中选择本机的 7z.exe。")
            return None
        return executable

    def _preflight_combined(self, selected: list[str]) -> bool:
        if self._resolve_7zip() is None:
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
        file_iids = [iid for iid in self.rows if iid not in self.folder_archives]
        decisions = dict(zip(file_iids, plan_renames([self.rows[iid] for iid in file_iids])))
        for iid in selected:
            if iid in self.folder_archives:
                continue
            decision = decisions[iid]
            candidate = decision.target if decision.status == "rename" else decision.source
            base = extraction_base(candidate)
            if base is None:
                continue
            destination = (output_root or candidate.parent) / f"{base}_解压"
            if destination.exists() or destination.is_symlink():
                messagebox.showerror("目标目录已存在", f"避免覆盖，解压前请处理已有目录：{destination}")
                return False
        return True

    def _start_extract(self, selected: list[str]) -> None:
        executable = self._resolve_7zip()
        if executable is None:
            return
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
            args=(executable, jobs, output_root, passwords, self.delete_archives.get(), max_concurrent),
            daemon=True,
        )
        thread.start()

    def _extract_worker(
        self,
        executable: Path,
        jobs: list[ExtractionJob],
        output_root: Path | None,
        passwords: tuple[str, ...],
        delete_archives: bool,
        max_concurrent: int,
    ) -> None:
        engine = ExtractionEngine(executable, passwords, lambda message: self.events.put(("log", message)))
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
            summary = (
                f"完成：成功 {result.successful_jobs}/{result.outer_archives} 个任务，"
                f"失败 {result.failed_jobs} 个，累计 {result.layers} 层，"
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

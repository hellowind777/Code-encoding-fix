# GUI 工具：为 Code-encoding-fix 提供 Windows UTF-8 与 Git Bash 配置的 tkinter 界面
# 设计为尽量在无管理员权限下运行，提供路径检测、日志与进度反馈
# 兼容 Python 3.10，使用原生 tkinter 组件

import ctypes
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk
import json
from typing import Callable
from itertools import chain
import time

try:
    import winreg  # type: ignore
except ImportError:
    winreg = None  # 在非 Windows 环境下避免崩溃


PROFILE_MARKER_START = "# === Code-encoding-fix 配置（自动生成）开始 ==="
PROFILE_MARKER_END = "# === Code-encoding-fix 配置（自动生成）结束 ==="
BASH_MARKER_START = "# === Code-encoding-fix 配置（自动生成）开始 ==="
BASH_MARKER_END = "# === Code-encoding-fix 配置（自动生成）结束 ==="


class SetupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Code-encoding-fix 编码配置助手 v1.0 - 阿華(github:hellowind777)")
        self._apply_app_icon()
        # 默认窗口尺寸与最小尺寸同步下调，保持宽度不变、降低高度以更贴合 1080p 显示
        self.root.geometry("820x750")
        self.root.minsize(820, 750)
        self.root.withdraw()
        appdata_root = Path(os.environ.get("APPDATA", Path.home()))
        self._config_dir = appdata_root / "Code-encoding-fix"
        self._config_path = self._config_dir / "config.json"
        self._backup_root = self._config_path.parent / "backup"
        self._console_reg_backup_path = self._backup_root / "shell_reg.orig"
        self._console_log_buffer: list[tuple[str, str]] = []
        self._ps5_profile_path = Path.home() / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
        self._ps7_profile_path = Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        self._git_bashrc_path: Path | None = None

        self.style = ttk.Style()
        try:
            self.style.theme_use("vista")
        except tk.TclError:
            self.style.theme_use("clam")

        self._init_fonts()

        self.ps5_path_var = tk.StringVar()
        self.ps7_path_var = tk.StringVar()
        self.git_path_var = tk.StringVar()
        self.vscode_path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="待检测 Git Bash / Visual Studio Code")
        self.console_info_var = tk.StringVar(value="控制台编码：待检测")
        self.tool_info_var = tk.StringVar(value="工具配置：待检测")
        self.progress_var = tk.IntVar(value=0)
        self.is_running = False
        self._is_admin_cached = self._is_admin()
        self._ps5_available = False
        self._ps7_available = False
        self._ps5_exe: Path | None = None
        self._ps7_exe: Path | None = None
        self._git_exe: Path | None = None
        self._row_widgets: dict[str, dict[str, ttk.Widget]] = {}
        self._detect_cache = {}
        self._detect_cache_max = 64
        self._shell_marker_detail = {}
        self._tool_config_detail = {}
        self._registry_cache: dict[tuple[str, ...], list[Path]] = {}
        self._shortcut_cache: dict[tuple[str, ...], list[Path]] = {}
        self._detecting = False

        self._build_layout()
        self._apply_window_position()
        # 先记录应用启动，再进行 Shell 路径检测，保证日志顺序符合直觉
        self._log("应用已启动，准备检测 Shell 路径", "info")
        self._detect_all_paths_in_thread(log=True)
        self._refresh_env_tool_labels()
        self._update_restore_button_state()
        self._refresh_start_button_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.deiconify()

    def _apply_app_icon(self) -> None:
        """为窗口/任务栏设置应用图标（优先使用同目录的 .ico）。"""
        if not sys.platform.startswith("win"):
            return

        base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon_path = base_dir / "Code-encoding-fix.ico"
        if not icon_path.exists():
            return

        try:
            self.root.iconbitmap(default=str(icon_path))
        except tk.TclError:
            return

    def _reset_to_system_default(self) -> None:
        """恢复控制台 CodePage 到系统默认（不依赖备份），不再改写环境变量。"""
        if self.is_running:
            return
        confirm = self._show_modal(
            "恢复系统默认(不含工具)",
            "将删除控制台编码恢复到当前系统语言默认编码(936)，不再改写环境变量。\n\n是否继续？",
            kind="confirm",
            confirm_text="继续",
            cancel_text="取消",
        )
        if not confirm:
            return
        self.is_running = True
        self._set_buttons_state(False)
        threading.Thread(target=self._run_reset_default, daemon=True).start()

    def _run_reset_default(self) -> None:
        actions: list[tuple[str, str]] = []
        try:
            default_lang, default_lc_all, default_cp = self._system_default_locale()

            self._log_separator("恢复系统默认(不含工具)开始")
            self._log("控制台编码：", "info")

            console_logs = self._set_console_codepage_all(default_cp)
            for level, message in console_logs:
                if "error" in level:
                    self._log(message, level)
            self._log_separator("恢复系统默认(不含工具)结束")

            # 刷新检测与状态
            self._ui_call(self._detect_all_paths, False)
            self._ui_call(self._refresh_env_tool_labels)
            self._ui_call(self._refresh_config_status_label)
            self._ui_call(self._refresh_start_button_state)
        except Exception as exc:  # noqa: BLE001
            self._log(f"恢复系统默认失败(不含工具): {exc}", "error")
        finally:
            self.is_running = False
            self._ui_call(self._set_buttons_state, True)
            rs = self._runtime_status()
            console_lines = rs.get("console", [])
            summary_parts = [
                "恢复系统默认完成(不含工具)。",
                "",
                "当前控制台编码：",
            ]
            summary_parts.extend([f"• {line}" for line in console_lines] or ["• 未检测到控制台状态"])
            summary = "\n".join(summary_parts)
            self._ui_call(self._show_modal, "完成", summary, "info")

    def _pick_ui_font_family(self) -> str:
        # 避免指定西文字体导致中文回退（出现“字体不一致/中文发虚”）
        candidates = [
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "微软雅黑",
            "Segoe UI",
        ]

        try:
            families = set(tkfont.families(self.root))
        except Exception:
            families = set()

        for family in candidates:
            if family in families:
                return family

        try:
            return tkfont.nametofont("TkDefaultFont").cget("family")
        except Exception:
            return "TkDefaultFont"

    def _init_fonts(self) -> None:
        self.ui_font_family = self._pick_ui_font_family()

        try:
            default_font = tkfont.nametofont("TkDefaultFont")
            self.ui_font_size = int(default_font.cget("size"))
            default_font.configure(family=self.ui_font_family)
        except Exception:
            self.ui_font_size = 10

        try:
            self.style.configure(".", font=(self.ui_font_family, self.ui_font_size))
        except Exception:
            pass

        # 显式字体（保持原字号，仅替换 family）
        self.font_title = (self.ui_font_family, 16, "bold")
        self.font_subtitle = (self.ui_font_family, 10)
        self.font_log = (self.ui_font_family, 10)

    def _build_layout(self) -> None:
        header = ttk.Frame(self.root, padding="12 8")
        header.pack(fill="x")
        ttk.Label(
            header,
            text="Code-encoding-fix 编码配置助手",
            font=self.font_title,
        ).pack(anchor="center")
        ttk.Label(
            header,
            text="一键修复Codex for Windows 运行乱码问题，完成 工具/shell UTF-8 编码配置",
            font=self.font_subtitle,
            foreground="#555",
        ).pack(anchor="center", pady=(1, 0))

        path_frame = ttk.LabelFrame(self.root, text="工具配置", padding="8")
        path_frame.pack(fill="x", padx=12, pady=4)
        path_frame.columnconfigure(1, weight=1)

        row_idx = 0
        row_idx = self._build_shell_row(
            parent=path_frame,
            key="ps5",
            label_text="Windows PowerShell 5.1",
            var=self.ps5_path_var,
            open_cmd=lambda: self._open_path("ps5"),
            start_row=row_idx,
        )
        row_idx = self._build_shell_row(
            parent=path_frame,
            key="ps7",
            label_text="PowerShell 7+",
            var=self.ps7_path_var,
            open_cmd=lambda: self._open_path("ps7"),
            start_row=row_idx,
        )
        row_idx = self._build_shell_row(
            parent=path_frame,
            key="git",
            label_text="Git Bash",
            var=self.git_path_var,
            open_cmd=lambda: self._open_path("git"),
            start_row=row_idx,
        )
        row_idx = self._build_shell_row(
            parent=path_frame,
            key="vscode",
            label_text="Visual Studio Code",
            var=self.vscode_path_var,
            open_cmd=lambda: self._open_path("vscode"),
            start_row=row_idx,
            readonly=True,
        )

        console_frame = ttk.LabelFrame(self.root, text="控制台配置", padding="8")
        console_frame.pack(fill="x", padx=12, pady=(2, 4))
        ttk.Label(console_frame, textvariable=self.console_info_var, foreground="#444").pack(anchor="w")

        # 状态摘要条（独立放置在语言环境分区下方）
        status_frame = ttk.Frame(self.root, padding="10 2")
        status_frame.pack(fill="x", padx=0, pady=(0, 4))
        self.admin_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="#0063b1")
        self.admin_label.pack(side="left", anchor="w")
        ttk.Button(status_frame, text="重新检测", command=lambda: self._detect_all_paths_in_thread(log=True)).pack(side="right")

        progress_frame = ttk.Frame(self.root, padding="12 2 12 0")
        progress_frame.pack(fill="x")
        ttk.Label(progress_frame, text="执行进度").pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", pady=4)

        control_row = ttk.Frame(progress_frame)
        control_row.pack(fill="x", pady=(1, 0))
        self.start_btn = ttk.Button(control_row, text="开始执行配置", command=self._start_setup)
        self.start_btn.pack(side="left")
        self.reset_default_btn = ttk.Button(control_row, text="恢复系统默认(不含工具)", command=self._reset_to_system_default)
        self.reset_default_btn.pack(side="left", padx=(8, 0))
        self.restore_btn = ttk.Button(control_row, text="恢复配置", command=self._restore_configs)
        self.restore_btn.pack(side="left", padx=(8, 0))
        ttk.Button(control_row, text="退出", command=self.root.destroy).pack(side="right")
        self.backup_btn = ttk.Button(
            control_row,
            text="备份目录",
            command=self._open_backup_dir,
        )
        self.backup_btn.pack(side="right", padx=(0, 8))

        log_frame = ttk.LabelFrame(self.root, text="日志输出", padding="12")
        log_frame.pack(fill="both", expand=True, padx=12, pady=4)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, state="disabled", font=self.font_log
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("info", foreground="#222")
        self.log_text.tag_config("success", foreground="#0b6e35")
        self.log_text.tag_config("warning", foreground="#b8860b")
        self.log_text.tag_config("error", foreground="#b00020")
        # 右键菜单：清空日志
        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(label="清空日志", command=self._clear_log)
        self.log_text.bind("<Button-3>", self._show_log_menu)

        # 底部留白以保证布局呼吸感
        ttk.Frame(self.root, height=2).pack(fill="x")

    def _build_shell_row(
        self,
        parent: ttk.Frame,
        key: str,
        label_text: str,
        var: tk.StringVar,
        open_cmd,
        start_row: int,
        readonly: bool = False,
    ) -> None:
        ttk.Label(parent, text=label_text + ":", width=22).grid(
            row=start_row, column=0, sticky="e", padx=(0, 6), pady=(2, 0)
        )
        entry = ttk.Entry(parent, textvariable=var, state="readonly" if readonly else "normal")
        entry.grid(row=start_row, column=1, sticky="we", padx=(0, 6), pady=(2, 0))
        btn = ttk.Button(parent, text="打开", command=open_cmd, width=8)
        btn.grid(row=start_row, column=2, sticky="e", padx=(0, 0), pady=(2, 0))

        status_full = ttk.Label(parent, text="", foreground="#444", anchor="w", justify="left", wraplength=620)
        status_full.grid(row=start_row + 1, column=1, columnspan=2, sticky="w", pady=(0, 4))

        self._row_widgets[key] = {"entry": entry, "btn": btn, "status_full": status_full}
        return start_row + 2

    def _update_console_state_label(self) -> None:
        status_label = self._console_config_state()
        summary_full = self._console_status_summary()
        summary_short = self._console_status_summary(short=True)
        self._console_summary_short = summary_short
        self._console_summary_list = summary_short.split(" ") if summary_short else []
        self._console_config_status = status_label
        self.console_info_var.set(f"控制台编码：{summary_full}")

    def _refresh_env_tool_labels(self) -> None:
        # 语言环境改由注册表 CodePage 控制，实时显示当前检测结果
        # 语言环境提示已合并到控制台状态，不再单独显示
        self._env_summary_short = ""
        self._env_status_short = ""

        appdata = os.environ.get("APPDATA")
        settings_path = Path(appdata) / "Code" / "User" / "settings.json" if appdata else None
        # 尝试定位 Visual Studio Code 可执行文件
        vscode_exe = shutil.which("code") or shutil.which("code.cmd")
        vscode_path_display = None
        if vscode_exe:
            resolved = Path(vscode_exe).resolve()
            if resolved.name.lower() == "code.cmd":
                candidate = resolved.parent.parent / "Code.exe"
                vscode_path_display = candidate if candidate.exists() else resolved
            elif resolved.name.lower() == "code":
                candidate = resolved.parent / "Code.exe"
                vscode_path_display = candidate if candidate.exists() else resolved
            else:
                vscode_path_display = resolved
        if settings_path and settings_path.exists():
            exe_part = f"{vscode_path_display}" if vscode_path_display else None
            if exe_part:
                self.tool_info_var.set(f"已检测到 Visual Studio Code: {exe_part}")
            else:
                self.tool_info_var.set("已检测到 Visual Studio Code")
            self.vscode_path_var.set(str(settings_path))
        else:
            self.tool_info_var.set("未检测到 Visual Studio Code，无法定位配置文件")
            self.vscode_path_var.set("未检测到 Visual Studio Code，执行时将自动写入默认路径")

    def _append_console_logs(self, messages: list[tuple[str, str]]) -> None:
        if not messages:
            return
        self._console_log_buffer.extend(messages)

    def _file_marker_status(self, path: Path | None, start: str, end: str) -> str:
        """
        返回标记状态:
        - "full": 同时存在 start/end
        - "partial": 仅存在 start 或 end
        - "none": 未检测到
        - "error": 读取失败
        """
        if not path:
            return "none"
        try:
            if not path.exists():
                return "none"
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            try:
                self._log(f"读取 {path} 失败: {exc}", "warning")
            except Exception:
                pass
            return "error"
        has_start = start in text
        has_end = end in text
        if has_start and has_end:
            return "full"
        if has_start or has_end:
            return "partial"
        return "none"

    @staticmethod
    def _normalize_block_text(text: str) -> str:
        """归一化配置块文本，用于比较差异（忽略换行差异与行尾空格）。"""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in normalized.split("\n")]
        # 去掉首尾空行，避免误报
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    @staticmethod
    def _extract_marker_blocks(text: str, start: str, end: str) -> list[str]:
        """提取由 start/end 包裹的所有配置块（包含 start/end 行本身）。"""
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        return pattern.findall(text)

    @staticmethod
    def _expected_powershell_block() -> str:
        """当前工具写入 PowerShell Profile 的标准配置块（含标记）。"""
        return "\n".join(
            [
                PROFILE_MARKER_START,
                "chcp 65001 | Out-Null",
                "[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new()",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()",
                "$OutputEncoding = [System.Text.UTF8Encoding]::new()",
                "$PSDefaultParameterValues['Get-Content:Encoding']    = 'utf8'",
                "$PSDefaultParameterValues['Set-Content:Encoding']    = 'utf8'",
                "$PSDefaultParameterValues['Add-Content:Encoding']    = 'utf8'",
                "$PSDefaultParameterValues['Out-File:Encoding']       = 'utf8'",
                "$PSDefaultParameterValues['Select-String:Encoding']  = 'utf8'",
                "$PSDefaultParameterValues['Import-Csv:Encoding']     = 'utf8'",
                "$PSDefaultParameterValues['Export-Csv:Encoding']     = 'utf8'",
                "$PSDefaultParameterValues['*:Encoding']              = 'utf8'",
                '$env:LANG = "zh_CN.UTF-8"',
                PROFILE_MARKER_END,
            ]
        )

    @staticmethod
    def _expected_bash_block() -> str:
        """当前工具写入 Git Bash ~/.bashrc 的标准配置块（含标记）。"""
        return "\n".join(
            [
                BASH_MARKER_START,
                'export LANG="zh_CN.UTF-8"',
                'export LC_ALL="zh_CN.UTF-8"',
                'export LC_CTYPE="zh_CN.UTF-8"',
                'export LC_MESSAGES="zh_CN.UTF-8"',
                'if command -v chcp >/dev/null 2>&1; then chcp 65001 >/dev/null 2>&1; fi',
                "git config --global core.quotepath false",
                "git config --global i18n.commitencoding utf-8",
                "git config --global i18n.logoutputencoding utf-8",
                BASH_MARKER_END,
            ]
        )

    @staticmethod
    def _is_utf8_locale_value(value: object) -> bool:
        if not isinstance(value, str):
            return False
        return bool(re.search(r"utf-?8", value, flags=re.IGNORECASE))

    @staticmethod
    def _equivalent_powershell_profile(text: str) -> tuple[bool, str]:
        """在无工具标记块时，保守判断 PowerShell profile 是否已做 UTF-8 等效配置。"""
        has_input = bool(re.search(r"\[console\]::\s*inputencoding\s*=\s*.*utf8", text, flags=re.IGNORECASE))
        has_output = bool(re.search(r"\[console\]::\s*outputencoding\s*=\s*.*utf8", text, flags=re.IGNORECASE))
        has_outputencoding = bool(re.search(r"\$outputencoding\s*=\s*.*utf8", text, flags=re.IGNORECASE))
        has_psdefaults = ("$psdefaultparametervalues" in text.lower()) and (":encoding" in text.lower()) and ("utf8" in text.lower())
        has_chcp = bool(re.search(r"(?:^|\s)chcp\s+65001\b", text, flags=re.IGNORECASE))
        ok = has_input and has_output and (has_psdefaults or has_outputencoding or has_chcp)
        if not ok:
            return False, ""
        reasons: list[str] = []
        if has_chcp:
            reasons.append("检测到 chcp 65001")
        if has_outputencoding:
            reasons.append("检测到 $OutputEncoding=UTF-8")
        if has_psdefaults:
            reasons.append("检测到 PSDefaultParameterValues(Encoding)")
        return True, "；".join(reasons) if reasons else "检测到关键 UTF-8 设置"

    @staticmethod
    def _equivalent_bashrc(text: str) -> tuple[bool, str]:
        """在无工具标记块时，保守判断 bashrc 是否已做 UTF-8 等效配置。"""
        has_lang = bool(re.search(r"^\s*export\s+LANG\s*=\s*['\"]?.*utf-?8", text, flags=re.IGNORECASE | re.MULTILINE))
        has_lc_all = bool(re.search(r"^\s*export\s+LC_ALL\s*=\s*['\"]?.*utf-?8", text, flags=re.IGNORECASE | re.MULTILINE))
        lower = text.lower()
        has_git = ("core.quotepath" in lower) or ("i18n.commitencoding" in lower) or ("i18n.logoutputencoding" in lower)
        ok = has_lang and has_lc_all and has_git
        if not ok:
            return False, ""
        return True, "检测到 LANG/LC_ALL 为 UTF-8 且包含 git 编码配置"

    def _analyze_marker_block(
        self,
        path: Path | None,
        start: str,
        end: str,
        expected_block: str,
        equivalent_check: Callable[[str], tuple[bool, str]] | None = None,
    ) -> dict[str, object]:
        """分析配置文件中工具生成的配置块是否存在漂移（被手动改动/重复/截断）。"""
        if not path:
            return {"state": "missing", "summary": "未定位到配置文件路径"}
        try:
            if not path.exists():
                return {"state": "missing", "summary": "配置文件不存在"}
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            self._log(f"读取 {path} 失败: {exc}", "warning")
            return {"state": "unreadable", "summary": "读取失败"}

        has_start = start in text
        has_end = end in text
        if not has_start and not has_end:
            if equivalent_check:
                ok, reason = equivalent_check(text)
                if ok:
                    suffix = f"：{reason}" if reason else ""
                    return {"state": "ok", "summary": f"等效配置已存在（无工具标记块）{suffix}"}
                # 无标记且未命中等效 UTF-8 配置：对用户更友好的摘要，避免误导为“仅缺少标记”
                return {"state": "missing", "summary": "未发现 UTF-8 配置"}
            return {"state": "missing", "summary": "未检测到配置块标记"}
        if has_start ^ has_end:
            return {"state": "partial", "summary": "检测到部分标记(可能被截断)"}

        blocks = self._extract_marker_blocks(text, start, end)
        if not blocks:
            # 理论上 has_start/has_end 成立时不应为空，这里兜底处理为“部分标记”
            return {"state": "partial", "summary": "检测到标记但无法提取完整配置块"}
        if len(blocks) > 1:
            return {"state": "duplicate", "summary": f"检测到重复配置块({len(blocks)}个)"}

        actual = self._normalize_block_text(blocks[0])
        expected = self._normalize_block_text(expected_block)
        if actual == expected:
            return {"state": "ok", "summary": "与标准模板一致"}

        # 生成简短差异摘要：定位首个不一致行
        actual_lines = actual.split("\n")
        expected_lines = expected.split("\n")
        first_idx: int | None = None
        for idx in range(max(len(actual_lines), len(expected_lines))):
            a = actual_lines[idx] if idx < len(actual_lines) else "<缺失>"
            e = expected_lines[idx] if idx < len(expected_lines) else "<多余>"
            if a != e:
                first_idx = idx
                break
        if first_idx is None:
            summary = "与标准模板不一致"
        else:
            a = actual_lines[first_idx] if first_idx < len(actual_lines) else "<缺失>"
            e = expected_lines[first_idx] if first_idx < len(expected_lines) else "<多余>"
            summary = f"第{first_idx + 1}行不一致：期望 `{e}`，实际 `{a}`"
        return {"state": "modified", "summary": summary}

    def _detect_vscode_settings_drift(self) -> dict[str, object]:
        """检测 Visual Studio Code settings.json 是否满足工具期望的 UTF-8 配置。"""
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return {"state": "missing", "summary": "无法定位 APPDATA"}
        settings_path = Path(appdata) / "Code" / "User" / "settings.json"
        cache_key = None
        try:
            st = settings_path.stat()
            cache_key = ('vscode_drift', str(settings_path), getattr(st, 'st_mtime_ns', st.st_mtime), st.st_size)
        except FileNotFoundError:
            cache_key = ('vscode_drift', str(settings_path), 'missing')
        except OSError:
            cache_key = ('vscode_drift', str(settings_path), 'unreadable')
        if cache_key and hasattr(self, '_detect_cache') and isinstance(self._detect_cache, dict):
            cached = self._detect_cache.get(cache_key)
            if cached is not None:
                return cached
        def _ret(d):
            if cache_key and hasattr(self, '_detect_cache') and isinstance(self._detect_cache, dict):
                if len(self._detect_cache) >= getattr(self, '_detect_cache_max', 64):
                    self._detect_cache.clear()
                self._detect_cache[cache_key] = d
            return d
        if not settings_path.exists():
            return {"state": "missing", "summary": "未找到 settings.json"}

        data, err = self._load_json_relaxed(settings_path)
        if err or not isinstance(data, dict):
            return {"state": "unreadable", "summary": err or "解析失败"}

        issues: list[str] = []
        # 1) files.encoding：允许大小写差异，但仍坚持 utf8（无 BOM）
        if "files.encoding" not in data:
            issues.append("缺少 `files.encoding`")
        else:
            enc = data.get("files.encoding")
            if not isinstance(enc, str):
                issues.append(f"`files.encoding` 当前={enc!r}，期望='utf8'")
            elif enc.lower() != "utf8":
                issues.append(f"`files.encoding` 当前={enc!r}，期望='utf8'")

        # 2) autoGuessEncoding：必须为 true
        if "files.autoGuessEncoding" not in data:
            issues.append("缺少 `files.autoGuessEncoding`")
        elif data.get("files.autoGuessEncoding") is not True:
            issues.append(f"`files.autoGuessEncoding` 当前={data.get('files.autoGuessEncoding')!r}，期望=true")

        # 3) 默认终端：允许等效 PowerShell（不同版本/命名）
        if "terminal.integrated.defaultProfile.windows" not in data:
            issues.append("缺少 `terminal.integrated.defaultProfile.windows`")
        else:
            prof = data.get("terminal.integrated.defaultProfile.windows")
            if not isinstance(prof, str):
                issues.append(f"`terminal.integrated.defaultProfile.windows` 当前={prof!r}，期望为 PowerShell")
            elif "powershell" not in prof.lower():
                issues.append(f"`terminal.integrated.defaultProfile.windows` 当前={prof!r}，期望为 PowerShell")

        env = data.get("terminal.integrated.env.windows")
        if not isinstance(env, dict):
            issues.append("缺少 `terminal.integrated.env.windows`")
        else:
            for ek, ev in (("LANG", "zh_CN.UTF-8"), ("LC_ALL", "zh_CN.UTF-8")):
                v = env.get(ek)
                if not self._is_utf8_locale_value(v):
                    issues.append(f"`terminal.integrated.env.windows.{ek}` 当前={v!r}，期望为 UTF-8 locale（如 {ev!r}）")

        if not issues:
            return {"state": "ok", "summary": "关键键值与期望一致"}
        return _ret({"state": "modified", "summary": "；".join(issues[:3]) + ("；..." if len(issues) > 3 else "")})

    def _detect_console_codepage_drift(self, expected_cp: int = 65001) -> list[str]:
        """检测 HKCU\\Console 目标键的 CodePage 是否与期望一致，返回差异摘要列表。"""
        diffs: list[str] = []
        for label, path in self._console_targets():
            key_name = self._console_key_from_path(path)
            values = self._read_console_values(key_name)
            current = None if not values else values.get("CodePage")
            try:
                current_int = int(current) if current is not None else None
            except Exception:
                current_int = None
            if current_int is None:
                diffs.append(f"{label}: 未检测到 CodePage（期望 {expected_cp}）")
            elif current_int != expected_cp:
                diffs.append(f"{label}: CodePage={current_int}（期望 {expected_cp}）")
        return diffs

    def _log_config_drift_report(self) -> None:
        """输出“哪些内容被手动更改”的差异提示（用于启动自动检测与手动重新检测）。"""
        labels = {
            "ps5": "Windows PowerShell 5.1",
            "ps7": "PowerShell 7+",
            "git": "Git Bash",
            "vscode": "Visual Studio Code",
        }
        availability = {
            "ps5": getattr(self, "_ps5_available", False),
            "ps7": getattr(self, "_ps7_available", False),
            "git": self._git_exe is not None,
            "vscode": bool(getattr(self, "_vscode_available", False)) or self._detect_vscode_settings_drift().get("state") != "missing",
        }

        # 先触发一次检测，确保 detail 缓存可用
        self._detect_shell_config_status()
        details: dict[str, dict[str, object]] = getattr(self, "_tool_config_detail", {})

        console_lines: list[tuple[str, str]] = []

        # 控制台编码漂移：仅在“看起来曾执行过配置”时强调期望为 UTF-8
        should_expect_utf8 = bool(self._console_reg_backup_path.exists()) or self._has_any_original_backup()
        if should_expect_utf8:
            console_diffs = self._detect_console_codepage_drift(expected_cp=65001)
            for diff in console_diffs:
                console_lines.append(("warning", f"控制台编码: {diff}"))

        # 输出顺序：工具摘要（缺失/漂移）→ 控制台编码（漂移/缺失）
        # ===== 工具配置与控制台编码 =====
        # 漂移日志分级：missing 不视为手动改动，partial 可自动清理
        def _level_for_state(s):
            if s in ('ok',):
                return 'success'
            if s in ('missing',):
                return 'warning'
            if s in ('partial', 'duplicate', 'modified'):
                return 'warning'
            if s in ('unreadable', 'error'):
                return 'error'
            return 'info'

        def _brief_state(item):
            if not isinstance(item, dict):
                return '未检测'
            s = str(item.get('state') or 'unknown')
            summary = str(item.get('summary') or '').strip()
            if s == 'ok':
                return 'utf-8编码已正确配置'
            if s == 'missing':
                return '未检测到 UTF-8 配置（可能被删除或尚未配置）'
            if s == 'partial':
                return '残缺标记（可自动清理）'
            if s == 'duplicate':
                return '重复块（将自动去重）'
            if s == 'modified':
                return '已偏离（执行时将覆盖修复）'
            if s == 'unreadable':
                return '不可读取'
            return s

        for key, label in (
            ("ps5", "Windows PowerShell 5.1"),
            ("ps7", "PowerShell 7+"),
            ("git", "Git Bash"),
            ("vscode", "Visual Studio Code"),
        ):
            if not availability.get(key):
                continue
            item = details.get(key) if isinstance(details, dict) else None
            state = str((item or {}).get('state') or 'unknown')
            self._log(f"{label}: { _brief_state(item) }", _level_for_state(state))

        if console_lines:
            for level, message in console_lines:
                self._log(f"• {message}", level)
    def _vscode_backup_path(self) -> Path:
        # 采用与其他备份一致的固定命名
        return self._backup_root / "vscode.orig"

    def _system_default_locale(self) -> tuple[str, str, int]:
        """获取系统默认的 LANG/LC_ALL/CodePage，失败时回退到 936。"""
        try:
            # Python 3.15 将移除 getdefaultlocale()，改用 setlocale/getlocale/getencoding 系列 API
            try:
                locale.setlocale(locale.LC_ALL, "")
            except Exception:
                pass
            lang, enc = locale.getlocale() or (None, None)
        except Exception:
            lang, enc = (None, None)
        if not lang:
            fallback_lang = os.environ.get("LANG", "")
            lang = fallback_lang.split(".", 1)[0] if fallback_lang else "zh_CN"
        cp: int | None = None
        try:
            cp = int(ctypes.windll.kernel32.GetACP())
        except Exception:
            cp = None
        if cp is None:
            try:
                enc_pref = locale.getpreferredencoding(False)
                m = re.search(r"(\\d{3,5})", enc_pref or "")
                if m:
                    cp = int(m.group(1))
            except Exception:
                cp = None
        if cp is None:
            cp = 936
        encoding = enc or f"cp{cp}"
        lang_val = f"{lang}.{encoding}"
        lc_all_val = lang_val
        return lang_val, lc_all_val, cp

    def _is_system_default_env(self) -> bool:
        """判断当前 LANG/LC_ALL/CHCP 与控制台 CodePage 是否均为系统默认。"""
        _, _, default_cp = self._system_default_locale()
        env_ok = True  # 语言环境不再依赖环境变量

        def _console_ok(cp_expected: int) -> bool:
            for _label, path in self._console_targets():
                values = self._read_console_values(self._console_key_from_path(path))
                if values is None:
                    continue  # 视为默认
                code = values.get("CodePage")
                if code is None:
                    continue
                if code != cp_expected:
                    return False
            return True

        return env_ok and _console_ok(default_cp)

    @staticmethod
    def _strip_json_comments_and_trailing_commas(text: str) -> str:
        """去除注释/尾逗号并清理非法控制字符，避免误删字符串内的 //。"""
        out: list[str] = []
        i = 0
        in_str = False
        esc = False
        while i < len(text):
            ch = text[i]
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if not in_str and ch == "/" and nxt == "/":
                # 行注释，跳到行末
                i = text.find("\n", i)
                if i == -1:
                    break
                out.append("\n")
                i += 1
                continue
            if not in_str and ch == "/" and nxt == "*":
                # 块注释
                end = text.find("*/", i + 2)
                i = end + 2 if end != -1 else len(text)
                continue
            out.append(ch)
            if ch == "\"" and not esc:
                in_str = not in_str
            esc = (ch == "\\" and not esc and in_str)
            i += 1
        cleaned = "".join(out)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)  # 尾随逗号
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", cleaned)  # 清理控制字符（保留 \t\r\n）
        return cleaned

    def _append_vscode_block(self, raw_text: str) -> tuple[str, bool, str | None]:
        """删除冲突键后在末尾追加统一块；保留原注释和行序。

        返回 (新文本, 是否写入, 错误消息)；错误时不改动原文本。
        """
        marker_start = "// Code-encoding-fix block (do not remove)"
        marker_end = "// Code-encoding-fix block end"
        # 历史版本可能存在文案差异；重复执行时需一并清理，避免残留“仅注释块”
        orphan_comment_lines = {
            marker_start,
            marker_end,
            "// 自动猜测编码以兼容混合文件",
            "// 终端默认使用 PowerShell",
            "// VS Code 终端环境：统一 UTF-8",
            "// Visual Studio Code 终端环境：统一 UTF-8",
        }

        newline = "\r\n" if "\r\n" in raw_text else "\n"
        target_keys = [
            '"files.encoding"',
            '"files.autoGuessEncoding"',
            '"terminal.integrated.defaultProfile.windows"',
        ]
        target_env_key = '"terminal.integrated.env.windows"'

        # 1) 优先移除完整标记块（包含 start/end 行），解决重复块/旧块残留问题
        lines = raw_text.splitlines(keepends=True)
        cleaned_lines: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if marker_start in line:
                j = i + 1
                while j < len(lines) and (marker_end not in lines[j]):
                    j += 1
                if j < len(lines) and marker_end in lines[j]:
                    # 跳过整个块
                    i = j + 1
                    continue
            cleaned_lines.append(line)
            i += 1

        # 2) 清理残缺块的可识别片段（仅删除工具可识别的标记/注释行，避免误伤用户配置）
        lines = [ln for ln in cleaned_lines if ln.strip() not in orphan_comment_lines]

        new_lines: list[str] = []
        in_env = False
        brace_depth = 0

        for line in lines:
            stripped = line.strip()
            if in_env:
                brace_depth += line.count("{") - line.count("}")
                if brace_depth <= 0:
                    in_env = False
                continue
            if any(k in stripped for k in target_keys):
                continue
            if target_env_key in stripped:
                in_env = True
                brace_depth = line.count("{") - line.count("}")
                if brace_depth <= 0:
                    in_env = False
                continue
            new_lines.append(line)

        closing_idx = None
        for i in range(len(new_lines) - 1, -1, -1):
            if "}" in new_lines[i]:
                closing_idx = i
                break
        if closing_idx is None:
            return raw_text, False, "settings.json 缺少结束大括号"

        insert_pos = closing_idx
        j = closing_idx - 1
        while j >= 0:
            t = new_lines[j].strip()
            if t == "" or t.startswith("//"):
                j -= 1
                continue
            if not t.endswith(",") and t not in ("{", "["):
                new_lines[j] = new_lines[j].rstrip("\n").rstrip() + ",\n"
            break

        block = [
            f"    {marker_start}{newline}",
            f'    "files.encoding": "utf8",{newline}',
            f"    // 自动猜测编码以兼容混合文件{newline}",
            f'    "files.autoGuessEncoding": true,{newline}',
            f"    // 终端默认使用 PowerShell{newline}",
            f'    "terminal.integrated.defaultProfile.windows": "PowerShell",{newline}',
            f"    // Visual Studio Code 终端环境：统一 UTF-8{newline}",
            f'    "terminal.integrated.env.windows": {{{newline}',
            f'        "LANG": "zh_CN.UTF-8",{newline}',
            f'        "LC_ALL": "zh_CN.UTF-8"{newline}',
            f"    }}{newline}",
            f"    {marker_end}{newline}",
        ]

        new_lines = new_lines[:insert_pos] + block + new_lines[insert_pos:]
        new_text = "".join(new_lines)
        changed = new_text != raw_text
        return new_text, changed, None

    def _load_json_relaxed(self, path: Path) -> tuple[dict | None, str | None]:
        """宽松解析 Visual Studio Code settings.json，失败返回错误信息且不写入。"""
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return None, f"读取 {path} 失败: {exc}"
        # 1) 严格解析
        try:
            return json.loads(raw), None
        except Exception:
            pass
        # 2) 清理注释/尾逗号/控制字符后严格解析
        cleaned = self._strip_json_comments_and_trailing_commas(raw)
        try:
            return json.loads(cleaned), None
        except Exception:
            pass
        # 3) 清理后 strict=False 解析（放宽控制字符限制）
        try:
            return json.loads(cleaned, strict=False), None
        except Exception:
            pass
        # 4) 将残余控制字符转义为 \\uXXXX 再 strict=False 解析
        escaped = re.sub(r"[\x00-\x1F]", lambda m: "\\u%04x" % ord(m.group(0)), cleaned)
        try:
            return json.loads(escaped, strict=False), None
        except Exception as exc:  # noqa: BLE001
            return None, f"解析 {path} 失败: {exc}"

    @staticmethod
    def _read_user_env_reg(name: str) -> str | None:
        if not winreg:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
        except Exception:
            return None

    @staticmethod
    def _read_user_env(name: str) -> str | None:
        """优先读取 HKCU\\Environment 的持久值，再回退进程环境变量。"""
        reg_val = SetupApp._read_user_env_reg(name)
        if reg_val is not None and reg_val != "":
            return reg_val
        env_val = os.environ.get(name)
        return env_val if env_val else None

    def _apply_vscode_settings(self, apply: bool, log: bool = True) -> None:
        """合并/恢复 Visual Studio Code 用户级 UTF-8 配置。"""
        appdata = os.environ.get("APPDATA")
        if not appdata:
            if log:
                self._log("无法定位 APPDATA，跳过 Visual Studio Code 设置", "warning")
            return
        settings_path = Path(appdata) / "Code" / "User" / "settings.json"
        backup_path = self._vscode_backup_path()
        target_dir = settings_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        if apply:
            if settings_path.exists() and not backup_path.exists():
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(settings_path, backup_path)
                if log:
                    self._log(f"已创建 Visual Studio Code 原始配置备份: {backup_path}", "info")
            raw_text = settings_path.read_text(encoding="utf-8") if settings_path.exists() else "{\n}\n"
            new_text, changed, err = self._append_vscode_block(raw_text)
            if err:
                if log:
                    level = "warning" if "解析失败" in err else "info"
                    self._log(f"Visual Studio Code 设置未写入：{err}", level)
                return
            if changed:
                settings_path.write_text(new_text, encoding="utf-8")
                if log:
                    self._log(f"已写入 Visual Studio Code UTF-8 用户设置: {settings_path}", "success")
            else:
                if log:
                    self._log("Visual Studio Code UTF-8 设置已存在，无需追加", "info")
        else:
            if backup_path.exists():
                shutil.copy2(backup_path, settings_path)
                backup_path.unlink(missing_ok=True)
                self._vscode_restore_result = "restored"
                if log:
                    self._log("Visual Studio Code 已从原始配置备份恢复", "info")
            else:
                self._vscode_restore_result = "no-backup"
                if log:
                    self._log("未找到 Visual Studio Code 原始配置备份，跳过恢复", "warning")

    def _flush_console_logs(self) -> None:
        if not self._console_log_buffer:
            return
        for level, message in self._console_log_buffer:
            self._log(message, level)
        self._console_log_buffer.clear()

    def _console_targets(self) -> list[tuple[str, Path]]:
        targets: list[tuple[str, Path]] = []
        if self._ps5_available and self._ps5_exe:
            targets.append(("Windows PowerShell 5.1", self._ps5_exe))

        if self._ps7_available and self._ps7_exe:
            targets.append(("PowerShell 7+", self._ps7_exe))

        wt_path = self._find_windows_terminal()
        if wt_path:
            targets.append(("Windows Terminal", wt_path))
        cmd_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        if cmd_path.exists():
            targets.append(("CMD", cmd_path))
        return targets

    def _find_windows_terminal(self) -> Path | None:
        candidates: list[Path] = []
        wt_from_path = shutil.which("wt.exe")
        if wt_from_path:
            candidates.append(Path(wt_from_path))
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe"
        candidates.append(local)
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates.append(program_files / "WindowsApps" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "wt.exe")
        for path in candidates:
            if path and path.exists():
                return path
        return None

    @staticmethod
    def _console_key_from_path(path: Path) -> str:
        sanitized = str(path).replace(":", "").replace("\\", "_").replace(" ", "_")
        return sanitized

    def _load_console_reg_backup(self) -> dict:
        if not self._console_reg_backup_path.exists():
            return {}
        try:
            return json.loads(self._console_reg_backup_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _read_console_values(self, key_name: str) -> dict | None:
        if not winreg:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Console\\" + key_name) as key:
                values: dict[str, str | int] = {}
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    name, value, _ = winreg.EnumValue(key, index)
                    values[name] = value
                return values
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            self._log(f"读取控制台注册表失败: {exc}", "warning")
            return None

    def _write_console_values(self, key_name: str, values: dict) -> None:
        if not winreg:
            raise RuntimeError("winreg 不可用")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Console\\" + key_name) as key:
            for name, value in values.items():
                if isinstance(value, int):
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
                else:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def _delete_console_key(self, key_name: str) -> None:
        if not winreg:
            raise RuntimeError("winreg 不可用")
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Console\\" + key_name)
        except FileNotFoundError:
            return

    def _set_console_codepage_all(self, codepage: int) -> list[tuple[str, str]]:
        """强制将所有控制台目标的 CodePage 写为指定值（忽略备份），用于恢复系统默认。"""
        outputs: list[tuple[str, str]] = []
        for label, path in self._console_targets():
            key_name = self._console_key_from_path(path)
            try:
                self._write_console_values(key_name, {"CodePage": codepage})
                outputs.append(("success", f"{label} 已写入系统默认 CodePage {codepage}"))
            except PermissionError as exc:  # noqa: BLE001
                outputs.append(("error", f"{label} 写入系统默认 CodePage 失败（权限不足）: {exc}"))
            except Exception as exc:  # noqa: BLE001
                outputs.append(("error", f"{label} 写入系统默认 CodePage 失败: {exc}"))
        self._update_console_state_label()
        return outputs

    def _update_console_codepage(
        self, apply_utf8: bool, *, emit_log: bool = True, fallback_cp: int | None = None
    ) -> list[tuple[str, str]]:
        reg_backup_loaded = self._load_console_reg_backup()
        outputs: list[tuple[str, str]] = []
        reg_backup_new = {}
        for label, path in self._console_targets():
            key_name = self._console_key_from_path(path)
            try:
                if apply_utf8:
                    existing = self._read_console_values(key_name) or {}
                    # 仅备份 CodePage，避免覆盖用户字体设置
                    reg_backup_new[key_name] = {"CodePage": existing.get("CodePage")} if existing else "__EMPTY_BACKUP__"
                    self._write_console_values(key_name, {"CodePage": 65001})
                    outputs.append(("success", f"{label} 控制台已设置为 UTF-8 代码页"))
                else:
                    backup = reg_backup_loaded.get(key_name, None)
                    if backup == "__EMPTY_BACKUP__":
                        if fallback_cp is not None:
                            self._write_console_values(key_name, {"CodePage": fallback_cp})
                            outputs.append(("info", f"{label} 原始未配置UTF-8，已写入系统默认 CodePage {fallback_cp}"))
                        else:
                            self._delete_console_key(key_name)
                            outputs.append(("info", f"{label} 原始未配置UTF-8，已清理当前设置"))
                    elif isinstance(backup, dict):
                        self._write_console_values(key_name, backup)
                        outputs.append(("success", f"{label} 控制台已恢复到原始设置"))
                    else:
                        if fallback_cp is not None:
                            self._write_console_values(key_name, {"CodePage": fallback_cp})
                            outputs.append(("warning", f"{label} 未找到原始配置备份，已写入系统默认 CodePage {fallback_cp}"))
                        else:
                            self._delete_console_key(key_name)
                            outputs.append(("warning", f"{label} 未找到原始配置备份，已清理为系统默认"))
            except PermissionError as exc:  # noqa: BLE001
                outputs.append(("error", f"{label} 控制台写入失败（权限不足）: {exc}"))
            except Exception as exc:  # noqa: BLE001
                outputs.append(("error", f"{label} 控制台写入失败: {exc}"))
        if apply_utf8:
            try:
                self._console_reg_backup_path.parent.mkdir(parents=True, exist_ok=True)
                # 仅在首次执行时写入原始备份；后续执行不覆盖，确保“恢复配置”始终回到首次执行前状态
                if not self._console_reg_backup_path.exists():
                    self._console_reg_backup_path.write_text(
                        json.dumps(reg_backup_new, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except Exception:
                pass
        self._update_console_state_label()
        if emit_log:
            for level, message in outputs:
                self._log(message, level)
        return outputs

    def _console_status_summary(self, short: bool = False) -> str:
        def label(name: str) -> str:
            return name

        statuses: list[str] = []

        # Windows PowerShell 5.1
        if self._ps5_available and self._ps5_exe:
            key_name = self._console_key_from_path(self._ps5_exe)
            values = self._read_console_values(key_name)
            if values and values.get("CodePage") == 65001:
                statuses.append(f"{label("Windows PowerShell 5.1")}=UTF-8")
            elif values and values.get("CodePage"):
                cp = values.get("CodePage")
                prefix = "UTF-8" if cp == 65001 else f"{cp}"
                statuses.append(f"{label("Windows PowerShell 5.1")}={prefix}")
            else:
                statuses.append(f"{label("Windows PowerShell 5.1")}=未配置UTF-8")
        else:
            statuses.append(f"{label("Windows PowerShell 5.1")}=未检测到")

        # PowerShell 7+
        if self._ps7_available and self._ps7_exe:
            key_name = self._console_key_from_path(self._ps7_exe)
            values = self._read_console_values(key_name)
            if values and values.get("CodePage") == 65001:
                statuses.append(f"{label("PowerShell 7+")}=UTF-8")
            elif values and values.get("CodePage"):
                cp = values.get("CodePage")
                prefix = "UTF-8" if cp == 65001 else f"{cp}"
                statuses.append(f"{label("PowerShell 7+")}={prefix}")
            else:
                statuses.append(f"{label("PowerShell 7+")}=未配置UTF-8")
        else:
            statuses.append(f"{label("PowerShell 7+")}=未安装")

        # Windows Terminal
        wt_path = self._find_windows_terminal()
        if wt_path:
            key_name = self._console_key_from_path(wt_path)
            values = self._read_console_values(key_name)
            if values and values.get("CodePage") == 65001:
                statuses.append(f"{label("Windows Terminal")}=UTF-8")
            elif values and values.get("CodePage"):
                cp = values.get("CodePage")
                prefix = "UTF-8" if cp == 65001 else f"{cp}"
                statuses.append(f"{label("Windows Terminal")}={prefix}")
            else:
                statuses.append(f"{label("Windows Terminal")}=未配置UTF-8")
        else:
            statuses.append(f"{label("Windows Terminal")}=未检测到")

        # CMD 状态单独追加
        cmd_cp, cmd_available = self._detect_cmd_codepage()
        if not cmd_available:
            statuses.append(f"{label("CMD")}=未检测到")
        elif cmd_cp == 65001:
            statuses.append(f"{label("CMD")}=65001")
        elif cmd_cp:
            statuses.append(f"{label("CMD")}={cmd_cp}")
        else:
            statuses.append(f"{label("CMD")}=未配置UTF-8")

        sep = " " if short else "；"
        return sep.join(statuses)

    def _runtime_status(self) -> dict[str, list[str] | str]:
        """汇总控制台编码与标记完整度，供状态栏/弹窗复用。"""
        console_list = [part.strip() for part in self._console_status_summary().split("；") if part.strip()]
        marker_detail = getattr(self, "_shell_marker_detail", {})
        labels = {
            "ps5": "Windows PowerShell 5.1",
            "ps7": "PowerShell 7+",
            "git": "Git Bash",
            "vscode": "Visual Studio Code",
        }
        marker_lines: list[str] = []
        for key, marker in marker_detail.items():
            label = labels.get(key, key)
            if marker in {"ok", "full"}:
                marker_lines.append(f"{label}: 配置一致")
            elif marker == "partial":
                marker_lines.append(f"{label}: 标记不完整，建议重新配置")
            elif marker == "duplicate":
                marker_lines.append(f"{label}: 检测到重复配置块，建议重新配置")
            elif marker == "modified":
                marker_lines.append(f"{label}: 检测到配置被改动，建议重新配置")
            elif marker in {"unreadable", "error"}:
                marker_lines.append(f"{label}: 读取/解析失败，详见日志")
            else:
                marker_lines.append(f"{label}: 未检测到工具配置")
        return {"console": console_list, "markers": marker_lines}

    def _env_status_summary(self) -> str:
        """占位：不再单独显示。"""
        return ""

    def _console_config_state(self, details: bool = False):
        """返回控制台配置状态：
        - status: 已全部配置UTF-8 / 已部分配置UTF-8 / 未配置UTF-8
        - available: 可检测的终端数量
        - configured: 已配置为 65001 的数量
        """
        available = 0
        configured = 0
        for _label, path in self._console_targets():
            key_name = self._console_key_from_path(path)
            values = self._read_console_values(key_name)
            if values is None:
                continue
            available += 1
            if values.get("CodePage") == 65001:
                configured += 1
        if available == 0:
            status = "未配置UTF-8"
        elif configured == available:
            status = "已全部配置UTF-8"
        elif configured > 0:
            status = "已部分配置UTF-8"
        else:
            status = "未配置UTF-8"
        return (status, available, configured) if details else status

    def _detect_cmd_codepage(self) -> tuple[int | None, bool]:
        """检测当前 CMD 默认代码页，返回 (codepage, cmd_available)。

        优先读取注册表 HKCU\\Environment\\CHCP（setx 写入的值，代表新开 CMD 默认值），
        再尝试 `cmd /c chcp` 输出，最后回退环境变量 CHCP。
        cmd_available 表示是否成功调用到 CMD 或取得任一来源信息。
        """
        cmd_available = False
        # 0) HKCU\Console 对 cmd.exe 的 CodePage（本工具主要写入点）
        try:
            cmd_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
            if cmd_path.exists():
                key = self._console_key_from_path(cmd_path)
                values = self._read_console_values(key)
                if values and values.get("CodePage"):
                    return int(values.get("CodePage")), True
        except Exception:
            pass
        # 1) registry (preferred, reflects setx 持久值)
        if winreg:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                    val, _ = winreg.QueryValueEx(key, "CHCP")
                    if str(val).isdigit():
                        return int(val), True
            except Exception:
                pass
        try:
            result = subprocess.run(
                ["cmd", "/c", "chcp"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout or result.stderr or "").strip()
            # 兼容中英文输出，提取数字
            match = re.search(r"(\d{3,5})", output)
            if match:
                cmd_available = True
                return int(match.group(1)), True
            cmd_available = True
        except Exception:
            pass
        env_cp = os.environ.get("CHCP")
        if env_cp and env_cp.isdigit():
            return int(env_cp), True
        return None, cmd_available

    def _all_consoles_utf8(self) -> bool:
        status, available, configured = self._console_config_state(details=True)
        return available > 0 and configured == available

    def _show_admin_warning(self, message: str) -> None:
        """管理员权限提示对话框，相对主窗口水平居中且垂直偏上。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("需要管理员权限")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        # 简单两行布局：左侧图标，右侧文本，下方“确定”按钮
        content = ttk.Frame(dialog, padding="16 12")
        content.grid(row=0, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)

        icon_label = ttk.Label(content, text="⚠", foreground="#d9534f", font=("Segoe UI", 20, "bold"))
        icon_label.grid(row=0, column=0, padx=(0, 12), sticky="n")

        msg_label = ttk.Label(content, text=message, justify="left", wraplength=360)
        msg_label.grid(row=0, column=1, sticky="w")

        def _on_close() -> None:
            dialog.grab_release()
            dialog.destroy()

        btn_frame = ttk.Frame(content)
        btn_frame.grid(row=1, column=0, columnspan=2, pady=(12, 0))
        ok_btn = ttk.Button(btn_frame, text="确定", command=_on_close)
        ok_btn.pack()

        dialog.update_idletasks()

        # 以主窗口为基准计算坐标：水平居中，垂直偏上（上半部分）
        min_w, min_h = self.root.minsize()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = self.root.winfo_width() or self.root.winfo_reqwidth() or min_w
        root_h = self.root.winfo_height() or self.root.winfo_reqheight() or min_h
        win_w = dialog.winfo_width()
        win_h = dialog.winfo_height()

        center_x = root_x + max(0, int((root_w - win_w) / 2))
        offset_y = root_y + max(0, int((root_h - win_h) / 4))
        dialog.geometry(f"{win_w}x{win_h}+{center_x}+{offset_y}")

        dialog.protocol("WM_DELETE_WINDOW", _on_close)
        ok_btn.focus_set()
        dialog.wait_window()

    def _set_row_state(
        self,
        key: str,
        enabled: bool,
        status: str,
        value: str | None = None,
        placeholder: bool = False,
    ) -> None:
        row = self._row_widgets.get(key)
        if not row:
            return
        if getattr(self, "_row_btn_state_locked", False):
            # 标记禁用期间已刷新行状态，避免恢复时覆盖检测结果
            self._row_btn_state_cache_dirty = True
        if value is not None:
            row["entry"].config(state="normal")
            row["entry"].delete(0, tk.END)
            row["entry"].insert(0, value)
            row["entry"].config(foreground="#888" if placeholder else "")
            row["entry"].config(state="readonly")
        state = "normal" if enabled else "disabled"
        row["btn"].config(state=state)
        row["status_full"].config(text=status)

    def _restore_row_buttons(self) -> None:
        """恢复禁用前的行按钮状态，集中管理以减少重复分支。"""
        # 若禁用期间已有检测逻辑刷新过行状态，则跳过恢复，保持最新检测结果
        if getattr(self, "_row_btn_state_cache_dirty", False):
            self._row_btn_state_cache.clear()
            self._row_btn_state_cache_dirty = False
            return

        for key, row in self._row_widgets.items():
            prev_state = self._row_btn_state_cache.get(key)
            if prev_state and row["btn"].cget("state") == "disabled":
                row["btn"].config(state=prev_state)
        self._row_btn_state_cache.clear()

    def _has_any_original_backup(self) -> bool:
        """检查是否存在任意 shell 的原始配置备份文件。"""
        try:
            locations = [self._backup_root]
            for root in locations:
                if not root.exists():
                    continue
                for name in ("ps5.orig", "ps7.orig", "git_bash.orig", "vscode.orig", "shell_reg.orig"):
                    if (root / name).exists():
                        return True
        except Exception:
            return False
        return False

    def _cleanup_backups(self) -> None:
        """删除所有原始配置备份文件，若目录为空则一并移除。"""
        try:
            root = self._backup_root
            if not root.exists():
                return
            for name in ("ps5.orig", "ps7.orig", "git_bash.orig", "vscode.orig", "shell_reg.orig"):
                file = root / name
                if file.exists():
                    try:
                        file.unlink()
                    except Exception:
                        pass
            # 若目录已空则删除目录，保持整洁
            try:
                next(root.iterdir())
            except StopIteration:
                try:
                    root.rmdir()
                except Exception:
                    pass
        except Exception:
            return

    def _update_restore_button_state(self, buttons_enabled: bool | None = None) -> None:
        """根据备份存在情况更新“恢复配置”按钮状态（有备份才可点击）。"""
        if not hasattr(self, "restore_btn"):
            return
        if buttons_enabled is None:
            buttons_enabled = not self.is_running
        # 只要存在原始配置备份且当前未在执行中，就允许点击；权限不足时在恢复时通过异常与日志反馈
        enabled = buttons_enabled and self._has_any_original_backup()
        state = "normal" if enabled else "disabled"
        self.restore_btn.config(state=state)

    # --------- 检测调度 ---------
    def _detect_all_paths_in_thread(self, log: bool = True) -> None:
        """后台线程执行检测，避免启动/重新检测阻塞 UI。"""
        if self.is_running or getattr(self, "_detecting", False):
            return
        self._detecting = True
        self.status_var.set("正在检测工具与编码状态...")
        self._set_buttons_state(False)
        threading.Thread(
            target=self._run_detect_all_paths_safe, args=(log,), daemon=True
        ).start()

    def _run_detect_all_paths_safe(self, log: bool) -> None:
        try:
            self._detect_all_paths(log=log)
        finally:
            self._ui_call(self._on_detect_done)

    def _on_detect_done(self) -> None:
        self._detecting = False
        self._set_buttons_state(True)
        self._refresh_start_button_state()

    def _detect_all_paths(self, log: bool = True) -> None:
        # 如果上一条日志是检测分隔线，先清理本次检测段落，避免重复追加
        t0 = time.perf_counter()
        if log:
            self._trim_last_detection_block()
            self._log_separator("检测开始")
        self._detect_ps5(log=log)
        self._detect_ps7(log=log)
        self._detect_git_paths(log=log)
        self._detect_vscode(log=log)
        if log:
            # 在检测块中输出“哪些内容被手动更改”的差异提示
            self._log_config_drift_report()
        if log:
            self._log(f"检测耗时 {time.perf_counter() - t0:.2f}s", "info")
            self._log_separator("检测结束")
        # 先刷新编码/环境，再基于结果刷新汇总与按钮状态
        self._update_console_state_label()
        self._refresh_env_tool_labels()
        self._refresh_config_status_label()
        self._refresh_start_button_state()
        self._refresh_reset_default_button_state()

    def _detect_ps5(self, log: bool = True) -> None:
        candidate = None
        which_ps = shutil.which("powershell")
        if which_ps:
            candidate = Path(which_ps).resolve()
        if candidate is None:
            sys_root = os.environ.get("SystemRoot", r"C:\Windows")
            candidate = Path(sys_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate is None or not candidate.exists():
            for target in self._shortcut_targets(["**/Windows PowerShell*.lnk"]):
                if target.exists():
                    candidate = target
                    break
        if candidate.exists():
            self._ps5_exe = candidate
            self._ps5_available = True
            profile_exists = self._ps5_profile_path.exists()
            path_text = str(self._ps5_profile_path) if profile_exists else "未找到配置文件，将在执行时创建"
            self.ps5_path_var.set(path_text)
            status = f"已检测到: {candidate}"
            self._set_row_state("ps5", profile_exists, status, path_text, placeholder=not profile_exists)
            if log:
                self._log(f"检测到 Windows PowerShell 5.1: {candidate}", "success")
        else:
            self._ps5_available = False
            self._ps5_exe = None
            self.ps5_path_var.set("")
            self._set_row_state(
                "ps5",
                False,
                "未检测到 Windows PowerShell 5.1，无法定位配置文件",
                "",
            )
            if log:
                self._log("未检测到 Windows PowerShell 5.1", "warning")

    def _detect_ps7(self, log: bool = True) -> None:
        path = None
        which_pwsh = shutil.which("pwsh")
        if which_pwsh:
            path = Path(which_pwsh).resolve()
        else:
            pf = os.environ.get("ProgramFiles", r"C:\Program Files")
            pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
            pf64 = os.environ.get("ProgramW6432", pf)
            search_roots = [pf, pf86, pf64]
            candidates: list[Path] = []
            for root in search_roots:
                if not root:
                    continue
                candidates.extend(sorted(Path(root).glob("PowerShell/*/pwsh.exe"), reverse=True))
            for c in candidates:
                if c.exists():
                    path = c
                    break
        if path is None:
            for loc in self._registry_install_locations(["powershell 7"]):
                candidate = Path(loc) / "pwsh.exe"
                if candidate.exists():
                    path = candidate
                    break
        if path is None:
            for target in self._shortcut_targets(["**/PowerShell 7*.lnk"]):
                if target.exists():
                    path = target
                    break
        if path and path.exists():
            self._ps7_exe = path
            self._ps7_available = True
            profile_exists = self._ps7_profile_path.exists()
            path_text = str(self._ps7_profile_path) if profile_exists else "未找到配置文件，将在执行时创建"
            self.ps7_path_var.set(path_text)
            status = f"已检测到: {path}"
            self._set_row_state("ps7", profile_exists, status, path_text, placeholder=not profile_exists)
            if log:
                self._log(f"检测到 PowerShell 7+: {path}", "success")
        else:
            self._ps7_available = False
            self._ps7_exe = None
            self.ps7_path_var.set("")
            self._set_row_state(
                "ps7",
                False,
                "未检测到 PowerShell 7+，无法定位配置文件",
                "",
            )
            if log:
                self._log("未检测到 PowerShell 7+", "warning")

    def _apply_window_position(self) -> None:
        cfg = self._load_config()
        if cfg:
            x, y = cfg.get("x"), cfg.get("y")
            if all(isinstance(v, int) and v > 0 for v in (x, y)):
                # 使用当前窗口尺寸，只应用保存的坐标
                self.root.update_idletasks()
                min_w, min_h = self.root.minsize()
                w = self.root.winfo_width() or min_w
                h = self.root.winfo_height() or min_h
                self.root.geometry(f"{w}x{h}+{x}+{y}")
                return
        self._center_window()

    def _center_window(self) -> None:
        self.root.update_idletasks()
        min_w, min_h = self.root.minsize()
        w = self.root.winfo_reqwidth() or self.root.winfo_width() or min_w
        h = self.root.winfo_reqheight() or self.root.winfo_height() or min_h
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = int((screen_w - w) / 2)
        y = int((screen_h - h) / 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _on_close(self) -> None:
        self._save_window_position()
        self.root.destroy()

    def _save_window_position(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            geom = self.root.winfo_geometry()
            size_part, _, pos_part = geom.partition("+")
            x_str, _, y_str = pos_part.partition("+")
            data = {
                "x": int(x_str),
                "y": int(y_str),
            }
            self._config_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

    def _load_config(self) -> dict:
        try:
            if self._config_path.exists():
                raw = json.loads(self._config_path.read_text(encoding="utf-8"))
                # 只保留位置坐标，忽略旧的宽高字段
                x = raw.get("x")
                y = raw.get("y")
                if isinstance(x, int) and isinstance(y, int):
                    return {"x": x, "y": y}
        except Exception:
            return {}
        return {}

    def _is_admin(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _ui_call(self, func: Callable, *args, **kwargs) -> None:
        """在主线程执行 UI 更新，避免后台线程直接操作 Tk 控件。"""
        self.root.after(0, lambda: func(*args, **kwargs))

    def _set_progress(self, percent: int) -> None:
        """安全设置进度条数值。"""
        self._ui_call(self.progress_var.set, max(0, min(100, percent)))

    def _progress_start(self, total_units: int) -> None:
        """初始化线性进度，单位粒度可自定义。"""
        self._progress_total_units = max(total_units, 1)
        self._progress_done_units = 0
        self._set_progress(0)

    def _progress_advance(self, units: int = 1) -> None:
        """按单位推进进度。"""
        if not hasattr(self, "_progress_total_units"):
            return
        self._progress_done_units = getattr(self, "_progress_done_units", 0) + units
        percent = int(self._progress_done_units * 100 / max(self._progress_total_units, 1))
        self._set_progress(percent)

    def _progress_finish(self) -> None:
        """收尾进度为 100%。"""
        self._set_progress(100)
        self._progress_total_units = 0
        self._progress_done_units = 0

    def _log(self, message: str, level: str = "info") -> None:
        tag = {"info": "info", "success": "success", "warning": "warning", "error": "error"}.get(
            level, "info"
        )

        def _append() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{level.upper()}] {message}\n", tag)
            self.log_text.configure(state="disabled")
            self.log_text.see("end")

        self.root.after(0, _append)

    def _log_separator(self, label: str) -> None:
        bar = "-" * 24
        self._log(f"{bar} {label} {bar}", "info")

    def _show_modal(
        self,
        title: str,
        message: str,
        kind: str = "info",
        confirm_text: str = "确定",
        cancel_text: str = "取消",
    ) -> bool:
        """自定义模态对话框，确保相对主窗口居中。返回 True/False。"""
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        icon_text = {"info": "ℹ", "warning": "⚠", "error": "✖"}.get(kind, "ℹ")
        fg = {"info": "#0b6e35", "warning": "#b8860b", "error": "#b00020"}.get(kind, "#222")

        frame = ttk.Frame(dialog, padding="16 12")
        frame.grid(row=0, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)

        ttk.Label(frame, text=icon_text, foreground=fg, font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, padx=(0, 12), sticky="n"
        )
        ttk.Label(frame, text=message, justify="left", wraplength=360).grid(row=0, column=1, sticky="w")

        result = {"value": False}

        def on_confirm() -> None:
            result["value"] = True
            dialog.destroy()

        def on_cancel() -> None:
            result["value"] = False
            dialog.destroy()

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=1, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btn_row, text=confirm_text, command=on_confirm, width=12).pack(side="left", padx=(0, 8))
        if kind == "confirm":
            ttk.Button(btn_row, text=cancel_text, command=on_cancel, width=12).pack(side="left")

        self.root.update_idletasks()
        dialog.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        min_w, min_h = self.root.minsize()
        root_w = self.root.winfo_width() or self.root.winfo_reqwidth() or min_w
        root_h = self.root.winfo_height() or self.root.winfo_reqheight() or min_h
        win_w = dialog.winfo_width() or 320
        win_h = dialog.winfo_height() or 180
        pos_x = root_x + max(0, int((root_w - win_w) / 2))
        pos_y = root_y + max(0, int((root_h - win_h) / 2))
        dialog.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        dialog.deiconify()
        dialog.lift(self.root)
        dialog.focus_set()
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        dialog.wait_window()
        return bool(result["value"])

    def _show_log_menu(self, event: tk.Event) -> None:  # type: ignore[override]
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _trim_last_detection_block(self) -> None:
        """若日志尾部为检测块，则移除尾部检测块（保留其他日志）；否则不处理。"""
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            return
        marker_start = "------------------------ 检测开始 ------------------------"
        marker_end = "------------------------ 检测结束 ------------------------"

        lines = content.splitlines(keepends=True)
        start_idx = end_idx = None
        for idx, line in enumerate(lines):
            if marker_start in line:
                start_idx = idx
            if marker_end in line:
                end_idx = idx

        if start_idx is None or end_idx is None or end_idx < start_idx:
            return
        # 仅当结束标记后无其他非空内容时才认为尾部是检测块
        if any(l.strip() for l in lines[end_idx + 1 :]):
            return

        kept = lines[:start_idx]
        new_content = "".join(kept).rstrip()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        if new_content:
            self.log_text.insert("end", new_content + "\n")
        self.log_text.configure(state="disabled")

    # --------- 通用候选路径收集工具 ---------
    def _registry_install_locations(self, keywords: list[str]) -> list[Path]:
        """从卸载注册表读取 InstallLocation，关键词大小写不敏感。"""
        key_tuple = tuple(sorted(k.lower() for k in keywords))
        if key_tuple in self._registry_cache:
            return list(self._registry_cache[key_tuple])
        if winreg is None:
            return []
        locations: list[Path] = []
        hives = [
            (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
            (winreg.HKEY_CURRENT_USER, "HKCU"),
        ]
        views = [0]
        if hasattr(winreg, "KEY_WOW64_64KEY"):
            views = [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]
        for hive, _ in hives:
            for view in views:
                try:
                    key = winreg.OpenKey(
                        hive,
                        r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                        0,
                        winreg.KEY_READ | view,
                    )
                except OSError:
                    continue
                try:
                    i = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            subkey = winreg.OpenKey(key, subkey_name)
                            display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                        except OSError:
                            continue
                        name_lower = str(display_name).lower()
                        if not any(k.lower() in name_lower for k in keywords):
                            continue
                        try:
                            loc, _ = winreg.QueryValueEx(subkey, "InstallLocation")
                        except OSError:
                            loc = ""
                        if loc:
                            p = Path(loc).expanduser()
                            if p.exists():
                                locations.append(p)
                finally:
                    try:
                        winreg.CloseKey(key)
                    except Exception:
                        pass
        seen = set()
        uniq: list[Path] = []
        for loc in locations:
            if loc not in seen:
                seen.add(loc)
                uniq.append(loc)
        self._registry_cache[key_tuple] = uniq
        return uniq

    def _shortcut_targets(self, patterns: list[str]) -> list[Path]:
        """解析开始菜单快捷方式目标路径（最佳努力，依赖 PowerShell COM）。"""
        pat_tuple = tuple(sorted(patterns))
        if pat_tuple in self._shortcut_cache:
            return list(self._shortcut_cache[pat_tuple])
        start_roots = [
            Path(os.environ.get("ProgramData", r"C:\ProgramData"))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs",
            Path(os.environ.get("APPDATA", Path.home()))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs",
        ]
        pwsh = shutil.which("powershell") or shutil.which("pwsh")
        if not pwsh:
            return []

        existing_roots = [str(r) for r in start_roots if r.exists()]
        if not existing_roots:
            self._shortcut_cache[pat_tuple] = []
            return []

        # 使用单次 PowerShell 批量解析，减少进程开销
        pattern_clause = " -or ".join([f"($_.Name -like '{p}')" for p in patterns])
        roots_literal = ",".join([f"'{r}'" for r in existing_roots])
        ps_script = ";".join(
            [
                "$ErrorActionPreference='SilentlyContinue'",
                f"$roots=@({roots_literal})",
                "$ws=New-Object -ComObject WScript.Shell",
                "$res=@()",
                "foreach($r in $roots){",
                " if(Test-Path $r){",
                "   Get-ChildItem -LiteralPath $r -Filter *.lnk -Recurse | Where-Object {"
                f" {pattern_clause} }} | ForEach-Object {{"
                "     $s=$ws.CreateShortcut($_.FullName);"
                "     if($s -and $s.TargetPath){ $res += $s.TargetPath }"
                "   }",
                " }",
                "}",
                "$res | Sort-Object -Unique",
            ]
        )
        targets: list[Path] = []
        try:
            proc = subprocess.run(
                [pwsh, "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.stdout:
                for line in proc.stdout.splitlines():
                    p = Path(line.strip()).expanduser()
                    if p.exists():
                        targets.append(p.resolve())
        except Exception:
            pass

        seen = set()
        uniq: list[Path] = []
        for t in targets:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        self._shortcut_cache[pat_tuple] = uniq
        return uniq

    def _detect_git_paths(self, log: bool = True) -> None:
        primary_paths: list[Path] = []
        env_paths = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramW6432"),
            os.environ.get("USERPROFILE"),
        ]
        for base in env_paths:
            if not base:
                continue
            base_path = Path(base)
            primary_paths.append(base_path / "Git")
            primary_paths.append(base_path / "AppData" / "Local" / "Programs" / "Git")

        bash_in_path = shutil.which("bash")
        if bash_in_path:
            primary_paths.append(Path(bash_in_path).resolve().parents[1])
        git_in_path = shutil.which("git")
        if git_in_path:
            git_root = Path(git_in_path).resolve().parent.parent
            primary_paths.append(git_root)

        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        secondary_paths = [
            Path(program_data) / "chocolatey" / "lib" / "git" / "tools",
            Path.home() / "scoop" / "apps" / "git" / "current",
            Path("D:/Programs/Git"),
            Path.home() / "AppData" / "Local" / "Programs" / "Git",
        ]

        found: Path | None = None
        for path in primary_paths:
            bash = path / "bin" / "bash.exe"
            if bash.exists():
                found = bash
                break

        if not found:
            # 仅在快速路径未命中时再做重扫描（注册表/快捷方式），避免启动慢
            for loc in self._registry_install_locations(["git for windows", "git version", "git"]):
                secondary_paths.append(loc)
            for target in self._shortcut_targets(["Git Bash*.lnk", "Git*.lnk"]):
                if target.name.lower().startswith("git-bash") or target.name.lower() == "bash.exe":
                    secondary_paths.append(target.parent.parent)
                else:
                    secondary_paths.append(target.parent)
            for path in secondary_paths:
                bash = path / "bin" / "bash.exe"
                if bash.exists():
                    found = bash
                    break

        if found:
            self._git_exe = found
            self._git_bashrc_path = Path.home() / ".bashrc"
            bashrc_path = self._git_bashrc_path
            bashrc_exists = bashrc_path.exists()
            path_text = str(bashrc_path) if bashrc_exists else "尚未发现 ~/.bashrc，将在执行时自动创建"
            self.git_path_var.set(path_text)
            status = f"已检测到 Git Bash: {found}"
            self._set_row_state("git", True, status, path_text, placeholder=not bashrc_exists)
            if log:
                self._log(f"检测到 Git Bash: {found}", "success")
        else:
            self._git_exe = None
            self.git_path_var.set("")
            self._set_row_state(
                "git",
                False,
                "未检测到 Git Bash，无法定位配置文件",
                "",
            )
            if log:
                self._log("未找到 Git Bash，无法配置 UTF-8，请先安装 Git for Windows", "warning")

    def _detect_vscode(self, log: bool = True) -> None:
        appdata = os.environ.get("APPDATA")
        settings_path = Path(appdata) / "Code" / "User" / "settings.json" if appdata else None
        exe_path = shutil.which("code") or shutil.which("code.cmd")
        exe_resolved = Path(exe_path).resolve() if exe_path else None
        if not exe_resolved:
            local_app = os.environ.get("LOCALAPPDATA")
            candidates = []
            if local_app:
                candidates.append(Path(local_app) / "Programs" / "Microsoft VS Code" / "Code.exe")
            program_files = [
                os.environ.get("ProgramFiles"),
                os.environ.get("ProgramFiles(x86)"),
                os.environ.get("ProgramW6432"),
            ]
            for root in program_files:
                if root:
                    candidates.append(Path(root) / "Microsoft VS Code" / "Code.exe")
            for loc in self._registry_install_locations(["visual studio code", "microsoft visual studio code"]):
                candidates.append(Path(loc) / "Code.exe")
            for target in self._shortcut_targets(["**/Visual Studio Code*.lnk"]):
                candidates.append(target)
            for c in candidates:
                if c and c.exists():
                    exe_resolved = c.resolve()
                    break
        self._vscode_available = bool(exe_resolved)
        display_exe = None
        if exe_resolved:
            if exe_resolved.name.lower() == "code.cmd":
                candidate = exe_resolved.parent.parent / "Code.exe"
                display_exe = candidate if candidate.exists() else exe_resolved
            elif exe_resolved.name.lower() == "code":
                candidate = exe_resolved.parent / "Code.exe"
                display_exe = candidate if candidate.exists() else exe_resolved
            else:
                display_exe = exe_resolved

        if display_exe:
            self.tool_info_var.set(f"已检测到 Visual Studio Code: {display_exe}")
        else:
            self.tool_info_var.set("未检测到 Visual Studio Code，无法定位配置文件")

        if settings_path:
            settings_exists = settings_path.exists()
            path_text = str(settings_path) if settings_exists else "未检测到 settings.json，执行时将自动写入默认路径"
            self.vscode_path_var.set(path_text if settings_exists else str(settings_path))
            status = (
                f"已检测到 Visual Studio Code: {display_exe}"
                if display_exe
                else "未检测到 Visual Studio Code，无法定位配置文件"
            )
            self._set_row_state(
                "vscode",
                settings_exists or bool(exe_resolved),
                status,
                path_text,
                placeholder=not settings_exists,
            )
        else:
            self.vscode_path_var.set("未检测到 Visual Studio Code，执行时将自动写入默认路径")
            self._set_row_state(
                "vscode",
                False,
                "未检测到 Visual Studio Code，无法定位配置文件",
                "",
                placeholder=True,
            )

        if log:
            if display_exe:
                self._log(f"检测到 Visual Studio Code: {display_exe}", "success")
            else:
                self._log("未检测到 Visual Studio Code 可执行文件", "warning")
    def _open_path(self, key: str) -> None:
        path_map = {
            "ps5": self._ps5_profile_path,
            "ps7": self._ps7_profile_path,
            "git": self._git_bashrc_path,
            "vscode": Path(self.vscode_path_var.get()) if self.vscode_path_var.get() else None,
        }
        path = path_map.get(key)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() and key in {"git", "vscode"}:
                path.touch()
            os.startfile(str(path))
        except Exception as exc:  # noqa: BLE001
            self._log(f"打开配置文件失败 {path}: {exc}", "error")
            self._show_modal("无法打开配置文件", f"路径: {path}\n错误: {exc}", kind="error")

    def _open_backup_dir(self) -> None:
        """打开软件备份目录，若不存在则创建。"""
        try:
            self._backup_root.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self._backup_root))
        except Exception as exc:  # noqa: BLE001
            self._log(f"打开备份目录失败 {self._backup_root}: {exc}", "error")
            self._show_modal("无法打开备份目录", f"路径: {self._backup_root}\n错误: {exc}", kind="error")

    def _validate_bash_path(self) -> Path | None:
        return self._git_exe

    def _start_setup(self) -> None:
        if self.is_running:
            return
        bash_path = self._validate_bash_path()
        if not bash_path:
            self._show_modal("错误", "未检测到有效的 bash.exe 路径", kind="error")
            return
        ps_profiles: list[tuple[Path, str]] = []
        if self.ps5_path_var.get():
            ps_profiles.append(
                (Path.home() / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1", "Windows PowerShell 5.1")
            )
        if self.ps7_path_var.get():
            ps_profiles.append(
                (Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1", "PowerShell 7+")
            )
        self.is_running = True
        self.status_var.set("执行中...")
        self._set_buttons_state(False)
        threading.Thread(target=self._run_setup, args=(bash_path, ps_profiles), daemon=True).start()

    def _set_buttons_state(self, enabled: bool) -> None:
        # 缓存各行按钮原始状态，结束时按需恢复，避免强制启用缺失工具的按钮
        if not hasattr(self, "_row_btn_state_cache"):
            self._row_btn_state_cache: dict[str, str] = {}
            self._row_btn_state_cache_dirty = False
            self._row_btn_state_locked = False
        self._update_restore_button_state(enabled)
        if not enabled:
            # 记录当前状态后统一禁用
            self._row_btn_state_cache = {k: row["btn"].cget("state") for k, row in self._row_widgets.items()}
            self._row_btn_state_cache_dirty = False
            self._row_btn_state_locked = True
            for row in self._row_widgets.values():
                row["btn"].config(state="disabled")
            if hasattr(self, "backup_btn"):
                self.backup_btn.config(state="disabled")
            return

        self._row_btn_state_locked = False
        self._restore_row_buttons()
        self._refresh_start_button_state(enabled)
        self._refresh_reset_default_button_state(enabled)
        if hasattr(self, "backup_btn"):
            self.backup_btn.config(state="normal")

    def _refresh_reset_default_button_state(self, baseline_enabled: bool = True) -> None:
        if not hasattr(self, "reset_default_btn"):
            return
        if not baseline_enabled or self.is_running:
            self.reset_default_btn.config(state="disabled")
            return
        state = "disabled" if self._is_system_default_env() else "normal"
        self.reset_default_btn.config(state=state)

    def _refresh_start_button_state(self, baseline_enabled: bool = True) -> None:
        """根据当前检测状态决定开始按钮是否可用。"""
        if not baseline_enabled:
            self.start_btn.config(state="disabled")
            return
        status = self._detect_shell_config_status()
        availability = {
            "ps5": getattr(self, "_ps5_available", False),
            "ps7": getattr(self, "_ps7_available", False),
            "git": self._git_exe is not None,
            "vscode": bool(getattr(self, "_vscode_available", False)) or self._detect_vscode_settings_drift().get("state") != "missing",
        }
        considered = {k: v for k, v in status.items() if availability.get(k)}
        all_configured = considered and all(considered.values())
        # 仅当可用的 shell 均已配置且控制台 CodePage 为 UTF-8 时禁用
        if all_configured and self._all_consoles_utf8():
            self.start_btn.config(state="disabled")
        else:
            self.start_btn.config(state="normal")

    def _run_setup(self, bash_path: Path, ps_profiles: list[tuple[Path, str]]) -> None:
        self._console_log_buffer.clear()
        ops: list[Callable[[], None]] = []

        def advance() -> None:
            self._progress_advance(1)

        # 进度条：每个操作前后各一次，保持与旧逻辑一致
        total_ops = len(ops)
        self._progress_start(total_ops * 2)

        self._log_separator("执行开始")
        self._log("工具配置：", "info")

        # 1) 校验 Git Bash
        ops.append(lambda: self._verify_bash(bash_path))

        # 2) 配置 Windows PowerShell 5.1
        if self.ps5_path_var.get():
            ps5_profile = Path.home() / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
            ops.append(lambda: self._configure_powershell_profile(ps5_profile, bash_path, "Windows PowerShell 5.1"))
        else:
            ops.append(lambda: self._log("Windows PowerShell 5.1: 未安装，跳过执行", "warning"))

        # 3) 配置 PowerShell 7+
        if self.ps7_path_var.get():
            ps7_profile = Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
            ops.append(lambda: self._configure_powershell_profile(ps7_profile, bash_path, "PowerShell 7+"))
        else:
            ops.append(lambda: self._log("PowerShell 7+: 未安装，跳过执行", "warning"))

        # 4) 配置 Git Bash
        ops.append(lambda: self._configure_bashrc_user(bash_path))

        # 5) 配置 Visual Studio Code
        ops.append(lambda: self._apply_vscode_settings(apply=True, log=True))

        # 6) 控制台编码
        def _console_utf8() -> None:
            self._log("控制台编码：", "info")
            backup_exists = self._console_reg_backup_path.exists()
            code_logs = self._update_console_codepage(apply_utf8=True, emit_log=False)
            if not backup_exists and self._console_reg_backup_path.exists():
                try:
                    data = json.loads(self._console_reg_backup_path.read_text(encoding="utf-8"))
                    placeholder = data and all(v == "__EMPTY_BACKUP__" for v in data.values())
                except Exception:
                    placeholder = False
                suffix = "占位（源文件不存在）" if placeholder else "原始配置备份"
                self._log(f"已创建 控制台编码 {suffix}: {self._console_reg_backup_path}", "info")
            logged: set[str] = set()
            # 按 PS5 -> PS7 -> WT 顺序输出；若 PS7 未安装追加警告
            for level, message in code_logs:
                if "PowerShell 7+" in message:
                    # 控制台编码部分只在实际存在时输出，缺失时后续补警告
                    self._log(message, level)
                    logged.add(message)
                elif "Windows PowerShell 5.1" in message:
                    self._log(message, level)
                    logged.add(message)
            if not (self._ps7_available and self._ps7_exe):
                self._log("PowerShell 7+: 未安装，跳过执行", "warning")
            for level, message in code_logs:
                if "Windows Terminal" in message:
                    self._log(message, level)
                    logged.add(message)
            for level, message in code_logs:
                if message in logged:
                    continue
                self._log(message, level)

        ops.append(_console_utf8)

        # 7) 刷新检测
        ops.append(lambda: self._ui_call(self._detect_all_paths, False))
        ops.append(lambda: self._ui_call(self._refresh_config_status_label))
        ops.append(lambda: self._ui_call(self._refresh_env_tool_labels))

        for action in ops:
            advance()
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                self._log(f"执行失败: {exc}", "error")
                self._ui_call(self.status_var.set, "执行中断，请查看日志")
                self._ui_call(self._refresh_config_status_label)
                self._flush_console_logs()
                self._finish(False)
                return
            advance()

        self._flush_console_logs()
        self._finish(True)
        self._log_separator("执行结束")

    def _finish(self, success: bool) -> None:
        """在主线程收尾并弹出提示，避免后台线程直接操作 UI。"""

        def _do_finish() -> None:
            self.is_running = False
            self._set_buttons_state(True)
            self._refresh_reset_default_button_state()
            if success:
                self._show_modal("完成", "配置完成，请重启 PowerShell / Git Bash / Visual Studio Code 后生效。", kind="info")
            else:
                self._show_modal("中断", "配置未全部完成，请检查日志。", kind="warning")

        self.root.after(0, _do_finish)

    def _verify_bash(self, bash_path: Path) -> None:
        if not bash_path.exists():
            raise FileNotFoundError(f"未找到 bash.exe: {bash_path}")
        result = subprocess.run(
            [str(bash_path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if result.returncode != 0:
            raise RuntimeError(f"bash --version 返回码 {result.returncode}")

    def _configure_powershell_profile(self, profile_path: Path, bash_path: Path, name: str) -> None:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        backup_key = "ps5" if "5.1" in name else "ps7"
        self._ensure_original_backup(profile_path, backup_key, name)
        existing = profile_path.read_text(encoding="utf-8", errors="ignore") if profile_path.exists() else ""
        ps_block = "\n".join(
            [
                PROFILE_MARKER_START,
                "chcp 65001 | Out-Null",
                "[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new()",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()",
                "$OutputEncoding = [System.Text.UTF8Encoding]::new()",
                "$PSDefaultParameterValues['Get-Content:Encoding']    = 'utf8'",
                "$PSDefaultParameterValues['Set-Content:Encoding']    = 'utf8'",
                "$PSDefaultParameterValues['Add-Content:Encoding']    = 'utf8'",
                "$PSDefaultParameterValues['Out-File:Encoding']       = 'utf8'",
                "$PSDefaultParameterValues['Select-String:Encoding']  = 'utf8'",
                "$PSDefaultParameterValues['Import-Csv:Encoding']     = 'utf8'",
                "$PSDefaultParameterValues['Export-Csv:Encoding']     = 'utf8'",
                "$PSDefaultParameterValues['*:Encoding']              = 'utf8'",
                '$env:LANG = "zh_CN.UTF-8"',
                PROFILE_MARKER_END,
                "",
            ]
        )
        content, cleaned_partial = self._strip_block_tolerant(existing, PROFILE_MARKER_START, PROFILE_MARKER_END, ps_block)
        if cleaned_partial:
            self._log("检测到残留半截标记（partial），已自动清理。", "info")
        if content != existing and existing:
            self._log(f"{name} 配置文件中检测到旧的 Code-encoding-fix 配置块，已清理后重新写入", "info")
        new_content = (content.strip() + "\n\n" + ps_block).strip() + "\n"
        profile_path.write_text(new_content, encoding="utf-8")
        self._log(f"已写入 {name} UTF-8 用户配置: {profile_path}", "success")

    def _configure_bashrc_user(self, bash_path: Path) -> None:
        if not bash_path.exists():
            raise FileNotFoundError(f"无法定位 bash.exe: {bash_path}")
        bashrc_path = self._git_bashrc_path or (Path.home() / ".bashrc")
        bashrc_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_original_backup(bashrc_path, "git_bash", "Git Bash")
        existing = bashrc_path.read_text(encoding="utf-8", errors="ignore") if bashrc_path.exists() else ""
        bash_block = "\n".join(
            [
                BASH_MARKER_START,
                'export LANG="zh_CN.UTF-8"',
                'export LC_ALL="zh_CN.UTF-8"',
                'export LC_CTYPE="zh_CN.UTF-8"',
                'export LC_MESSAGES="zh_CN.UTF-8"',
                'if command -v chcp >/dev/null 2>&1; then chcp 65001 >/dev/null 2>&1; fi',
                "git config --global core.quotepath false",
                "git config --global i18n.commitencoding utf-8",
                "git config --global i18n.logoutputencoding utf-8",
                BASH_MARKER_END,
                "",
            ]
        )
        content, cleaned_partial = self._strip_block_tolerant(existing, BASH_MARKER_START, BASH_MARKER_END, bash_block)
        if cleaned_partial:
            self._log("检测到残留半截标记（partial），已自动清理。", "info")
        if content != existing and existing:
            self._log("Git Bash 用户态配置文件中检测到旧的 Code-encoding-fix 配置块，已清理后重新写入", "info")
        bashrc_path.write_text((content.strip() + "\n\n" + bash_block).strip() + "\n", encoding="utf-8")
        self._log(f"已写入 Git Bash UTF-8 用户配置: {bashrc_path}", "success")

    def _ensure_original_backup(self, path: Path, key: str, display: str) -> None:
        """为配置文件创建首份原始配置备份，仅在备份不存在时执行。"""
        try:
            self._backup_root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            self._log(f"创建备份目录失败 {self._backup_root}: {exc}", "warning")
            return
        backup_path = self._backup_root / f"{key}.orig"
        if backup_path.exists():
            return
        empty_marker = "__EMPTY_BACKUP__"
        if not path.exists():
            try:
                backup_path.write_text(empty_marker, encoding="utf-8")
                self._log(f"已创建 {display} 原始配置备份占位（源文件不存在）: {backup_path}", "info")
            except Exception as exc:  # noqa: BLE001
                self._log(f"创建空占位备份失败 {backup_path}: {exc}", "warning")
            return
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            self._log(f"读取待备份文件失败 {path}: {exc}", "warning")
            return
        markers = {
            "ps5": (PROFILE_MARKER_START, PROFILE_MARKER_END),
            "ps7": (PROFILE_MARKER_START, PROFILE_MARKER_END),
            "git_bash": (BASH_MARKER_START, BASH_MARKER_END),
        }
        marker_pair = markers.get(key)
        if not content.strip():
            try:
                backup_path.write_text(empty_marker, encoding="utf-8")
                self._log(f"已创建 {display} 原始配置备份占位（源文件为空）: {backup_path}", "info")
            except Exception as exc:  # noqa: BLE001
                self._log(f"创建空占位备份失败 {backup_path}: {exc}", "warning")
            return
        if marker_pair and marker_pair[0] in content and marker_pair[1] in content:
            self._log(f"跳过备份（已是工具生成内容）: {path}", "info")
            return
        try:
            shutil.copy2(path, backup_path)
            self._log(f"已创建 {display} 原始配置备份: {backup_path}", "info")
        except Exception as exc:  # noqa: BLE001
            self._log(f"创建原始配置备份失败 {path} -> {backup_path}: {exc}", "warning")

    @staticmethod
    def _strip_block(content: str, start: str, end: str) -> str:
        """移除内容中由 start/end 包裹的所有配置块，用于保证幂等写入。"""
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        return re.sub(pattern, "", content)


    @staticmethod
    def _strip_block_tolerant(content, start, end, expected_block):
        """在仅出现 start 或 end 标记的情况下，尽量只清理工具可识别的残留片段，避免误删用户内容。"""
        if content is None:
            return content, False
        # 先清理完整块（幂等）
        content2 = SetupApp._strip_block(content, start, end)
        if content2 != content:
            content = content2
        # 构建期望行集合（用于保守匹配清理）
        exp_lines = []
        for line in str(expected_block).splitlines():
            s = line.strip()
            if not s:
                continue
            if s == str(start).strip() or s == str(end).strip():
                continue
            exp_lines.append(s)
        exp_set = set(exp_lines)
        if not exp_set:
            return content, False
        lines_local = content.splitlines(True)
        changed_any = False

        def _norm(x):
            return x.strip()

        # partial: start without end -> forward clean only expected lines
        i = 0
        while i < len(lines_local):
            if _norm(lines_local[i]) == str(start).strip() and all(_norm(l) != str(end).strip() for l in lines_local[i+1:]):
                j = i + 1
                while j < len(lines_local):
                    s = _norm(lines_local[j])
                    if not s or s in exp_set:
                        j += 1
                        continue
                    break
                del lines_local[i:j]
                changed_any = True
                continue
            i += 1

        # partial: end without start -> backward clean only expected lines
        i = 0
        while i < len(lines_local):
            if _norm(lines_local[i]) == str(end).strip() and all(_norm(l) != str(start).strip() for l in lines_local[:i]):
                j = i - 1
                while j >= 0:
                    s = _norm(lines_local[j])
                    if not s or s in exp_set:
                        j -= 1
                        continue
                    break
                del lines_local[j+1:i+1]
                changed_any = True
                i = 0
                continue
            i += 1

        return ''.join(lines_local), changed_any

    def _detect_shell_config_status(self) -> dict[str, bool]:
        """检测工具配置是否满足期望（支持识别被手动改动的漂移）。

        返回值仍为兼容旧逻辑的 dict[str, bool]（True 表示该项配置“正确且一致”）。
        详细状态写入 self._shell_marker_detail / self._tool_config_detail 供日志与摘要使用。
        """
        status: dict[str, bool] = {"ps5": False, "ps7": False, "git": False, "vscode": False}
        detail: dict[str, str] = {}
        tool_detail: dict[str, dict[str, object]] = {}

        expected_ps = self._expected_powershell_block()
        expected_git = self._expected_bash_block()
        bashrc_path = self._git_bashrc_path

        # 检测结果缓存：同一检测周期避免重复读取/解析
        def _path_sig(p):
            if not p:
                return ('none',)
            try:
                p_str = str(p)
            except Exception:
                p_str = repr(p)
            try:
                st = p.stat()
                return (p_str, getattr(st, 'st_mtime_ns', st.st_mtime), st.st_size)
            except FileNotFoundError:
                return (p_str, 'missing')
            except OSError:
                return (p_str, 'unreadable')

        cache_key = (
            "shell_status",
            _path_sig(self._ps5_profile_path),
            _path_sig(self._ps7_profile_path),
            _path_sig(bashrc_path),
            hash(expected_ps),
            hash(expected_git),
        )
        if hasattr(self, "_detect_cache") and isinstance(self._detect_cache, dict):
            cached = self._detect_cache.get(cache_key)
            if cached is not None:
                cached_status, cached_detail, cached_tool_detail = cached
                self._shell_marker_detail = cached_detail
                self._tool_config_detail = cached_tool_detail
                return cached_status

        for key, path in (("ps5", self._ps5_profile_path), ("ps7", self._ps7_profile_path)):
            analyzed = self._analyze_marker_block(
                path,
                PROFILE_MARKER_START,
                PROFILE_MARKER_END,
                expected_ps,
                equivalent_check=self._equivalent_powershell_profile,
            )
            state = str(analyzed.get("state", "missing"))
            detail[key] = state
            tool_detail[key] = analyzed
            status[key] = state == "ok"

        analyzed_git = self._analyze_marker_block(
            bashrc_path,
            BASH_MARKER_START,
            BASH_MARKER_END,
            expected_git,
            equivalent_check=self._equivalent_bashrc,
        )
        state_git = str(analyzed_git.get("state", "missing"))
        detail["git"] = state_git
        tool_detail["git"] = analyzed_git
        status["git"] = state_git == "ok"

        analyzed_vscode = self._detect_vscode_settings_drift()
        state_vscode = str(analyzed_vscode.get("state", "missing"))
        detail["vscode"] = state_vscode
        tool_detail["vscode"] = analyzed_vscode
        status["vscode"] = state_vscode == "ok"

        self._shell_marker_detail = detail
        self._tool_config_detail = tool_detail

        if hasattr(self, "_detect_cache") and isinstance(self._detect_cache, dict):
            if len(self._detect_cache) >= getattr(self, "_detect_cache_max", 64):
                self._detect_cache.clear()
            self._detect_cache[cache_key] = (status, detail, tool_detail)
        return status

    def _refresh_config_status_label(self) -> None:
        """根据配置状态刷新路径区域的汇总提示。"""
        status = self._detect_shell_config_status()
        labels = {
            "ps5": "Windows PowerShell 5.1",
            "ps7": "PowerShell 7+",
            "git": "Git Bash",
            "vscode": "Visual Studio Code",
        }

        availability = {
            "ps5": getattr(self, "_ps5_available", False),
            "ps7": getattr(self, "_ps7_available", False),
            "git": self._git_exe is not None,
            "vscode": bool(getattr(self, "_vscode_available", False)) or self._detect_vscode_settings_drift().get("state") != "missing",
        }
        considered = {k: v for k, v in status.items() if availability.get(k)}

        configured = [labels[k] for k, v in considered.items() if v]
        missing = [labels[k] for k, v in considered.items() if not v]

        if all(considered.values()):
            tools_status = "已全部配置UTF-8"
        elif any(considered.values()):
            tools_status = "已部分配置UTF-8"
        else:
            tools_status = "未配置UTF-8"

        console_status_list = getattr(self, "_console_summary_list", [])
        console_status = getattr(self, "_console_config_status", "未配置UTF-8")

        tools_text = f"工具：{tools_status}"
        console_text = f"编码：{console_status}"
        line = " | ".join([tools_text, console_text])

        # 漂移提示：用于快速定位“哪些内容被手动改动”
        drift_labels: list[str] = []
        detail = getattr(self, "_shell_marker_detail", {})
        for k in considered.keys():
            state = detail.get(k, "")
            if state in {"partial", "duplicate", "modified", "unreadable", "error"}:
                drift_labels.append(labels.get(k, k))
        if drift_labels:
            shown = drift_labels[:4]
            suffix = f"等{len(drift_labels)}项" if len(drift_labels) > 4 else ""
            line = f"{line}（检测到改动: {', '.join(shown)}{suffix}）"

        line = f"{line}。 请重新打开终端/shell工具以应用设置！"
        self.status_var.set(line)

        # 工具行内展示：漂移结论 + 简要原因（避免误导为手动改动）
        def _brief(item):
            if not isinstance(item, dict):
                return '未检测'
            state = str(item.get('state') or 'unknown')
            summary = str(item.get('summary') or '').strip()
            if state == 'ok':
                return 'utf-8编码已正确配置'
            if state == 'missing':
                return '未检测到 UTF-8 配置（可能被删除或尚未配置）'
            if state == 'partial':
                return '残缺标记（可自动清理）'
            if state == 'duplicate':
                return '重复块（将自动去重）'
            if state == 'modified':
                return '已偏离（执行时将覆盖修复）'
            if state == 'unreadable':
                return '不可读取'
            if summary:
                return summary
            return state

        for key in ('ps5', 'ps7', 'git', 'vscode'):
            row = getattr(self, '_row_widgets', {}).get(key)
            if not isinstance(row, dict):
                continue
            w = row.get('status_full')
            if w is None:
                continue
            try:
                w.config(text=_brief(getattr(self, '_tool_config_detail', {}).get(key)))
            except Exception:
                pass
    def _restore_configs(self) -> None:
        """恢复 PowerShell 与 Git Bash 配置到原始配置备份版本（后台线程执行，避免卡 UI）。"""
        if self.is_running:
            return
        self._log_separator("恢复开始")
        self._restore_start_logged = True
        self._set_buttons_state(False)
        confirm = self._show_modal(
            "恢复配置",
            "将 工具配置、控制台编码和shell语言环境 恢复到首次执行时的状态，会覆盖之后的手动修改。\n\n是否继续？",
            kind="confirm",
            confirm_text="继续",
            cancel_text="取消",
        )
        if not confirm:
            self._log("恢复已取消", "warning")
            self._log_separator("恢复结束")
            self._set_buttons_state(True)
            return

        self.is_running = True
        threading.Thread(target=self._run_restore, daemon=True).start()

    def _run_restore(self) -> None:
        """后台执行恢复逻辑，完成后调回主线程更新 UI。"""
        try:
            ps5_profile = Path.home() / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
            ps7_profile = Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
            bashrc_path: Path = self._git_bashrc_path or (Path.home() / ".bashrc")
            default_lang, default_lc_all, default_cp = self._system_default_locale()

            def find_backup(key: str) -> Path | None:
                candidates = [self._backup_root / f"{key}.orig"]
                candidates.extend(self._backup_root.glob(f"{key}.orig*"))
                for c in candidates:
                    if c.exists():
                        return c
                return None

            def restore_one(path: Path, key: str, display: str, allow_delete_if_no_backup: bool = False) -> tuple[str, str]:
                backup_path = find_backup(key)
                if not backup_path:
                    if allow_delete_if_no_backup and path.exists():
                        try:
                            path.unlink()
                            return "success", f"{display}: 原始为空，已删除当前配置文件"
                        except Exception as exc:  # noqa: BLE001
                            return "warning", f"{display}: 尝试删除配置文件失败 {exc}"
                    return "warning", f"{display}: 未找到原始配置备份，跳过"
                try:
                    content = backup_path.read_text(encoding="utf-8", errors="ignore")
                except Exception as exc:  # noqa: BLE001
                    return "warning", f"{display}: 读取备份失败 {exc}"
                if content == "__EMPTY_BACKUP__":
                    try:
                        if path.exists():
                            path.unlink()
                            return "success", f"{display}: 原始为空，已删除当前配置文件"
                        return "success", f"{display}: 原始为空，无需删除当前配置文件"
                    except Exception as exc:  # noqa: BLE001
                        return "warning", f"{display}: 删除文件失败 {exc}"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path, path)
                    return "success", f"{display}: 已从原始配置备份恢复"
                except Exception as exc:  # noqa: BLE001
                    return "warning", f"{display}: 恢复失败 {exc}"

            tool_logs: list[tuple[str, str]] = []

            self._progress_start(14)  # 7 个关键动作，前后推进

            self._progress_advance(1)
            if self._ps5_available or self._ps5_exe:
                tool_logs.append(restore_one(ps5_profile, "ps5", "Windows PowerShell 5.1", True))
            else:
                tool_logs.append(("warning", "Windows PowerShell 5.1: 未安装，跳过恢复"))
            self._progress_advance(1)

            self._progress_advance(1)
            if getattr(self, "_ps7_available", False) and self._ps7_exe:
                tool_logs.append(restore_one(ps7_profile, "ps7", "PowerShell 7+", True))
            else:
                tool_logs.append(("warning", "PowerShell 7+: 未安装，跳过恢复"))
            self._progress_advance(1)

            self._progress_advance(1)
            tool_logs.append(restore_one(bashrc_path, "git_bash", "Git Bash", True))
            self._progress_advance(1)

            self._progress_advance(1)
            self._apply_vscode_settings(apply=False, log=False)
            vscode_log = (
            ("success", "Visual Studio Code 已从原始配置备份恢复")
                if getattr(self, "_vscode_restore_result", "") == "restored"
            else ("warning", "Visual Studio Code 未找到原始备份，未改动当前配置文件")
                if getattr(self, "_vscode_restore_result", "") == "no-backup"
            else ("warning", "Visual Studio Code: 未安装，跳过恢复") if not getattr(self, "_vscode_available", False) else ("info", "Visual Studio Code: 已检测到，可手动检查 settings.json")
            )
            tool_logs.append(vscode_log)
            self._progress_advance(1)

            self._progress_advance(1)
            console_logs = self._update_console_codepage(apply_utf8=False, emit_log=False, fallback_cp=default_cp)
            self._progress_advance(1)

            self._progress_advance(1)
            self._cleanup_backups()
            self._progress_advance(1)

            self._progress_advance(1)
            self._ui_call(self._detect_all_paths, False)
            self._ui_call(self._refresh_config_status_label)
            self._ui_call(self._refresh_env_tool_labels)
            self._progress_advance(1)

            if not getattr(self, "_restore_start_logged", False):
                self._log_separator("恢复开始")
            self._restore_start_logged = False

            self._log("工具配置：", "info")
            for level, msg in tool_logs:
                self._log(msg, level)

            self._log("控制台编码：", "info")
            if self._ps5_available and self._ps5_exe:
                self._log(f"Windows PowerShell 5.1 已恢复原始值 CodePage {default_cp}", "success")
            else:
                self._log("Windows PowerShell 5.1: 未安装，跳过恢复", "warning")
            if not (self._ps7_available and self._ps7_exe):
                self._log("PowerShell 7+: 未安装，跳过恢复", "warning")
            else:
                self._log(f"PowerShell 7+ 已恢复原始值 CodePage {default_cp}", "success")
            if self._find_windows_terminal():
                self._log(f"Windows Terminal 已恢复原始值 CodePage {default_cp}", "success")
            else:
                self._log("Windows Terminal: 未检测到，跳过恢复", "warning")
            self._log(f"CMD 已恢复原始值 CodePage {default_cp}", "success")
            for level, message in console_logs:
                if "error" in level:
                    self._log(message, level)

            self._log_separator("执行结束")

            # 组装完成摘要
            header = "恢复完成，请重新检测或重新执行配置以生效。"
            tool_lines: list[str] = []
            tool_lines.append("• Windows PowerShell 5.1: 已从原始配置备份恢复" if self._ps5_available else "• Windows PowerShell 5.1: 未安装，跳过恢复")
            tool_lines.append("• PowerShell 7+: 已从原始配置备份恢复" if getattr(self, "_ps7_available", False) else "• PowerShell 7+: 未安装，跳过恢复")
            tool_lines.append("• Git Bash: 已从原始配置备份恢复" if self._git_exe else "• Git Bash: 未安装，跳过恢复")
            if getattr(self, "_vscode_available", False):
                if getattr(self, "_vscode_restore_result", "") == "restored":
                    tool_lines.append("• Visual Studio Code: 已从原始配置备份恢复")
                elif getattr(self, "_vscode_restore_result", "") == "no-backup":
                    tool_lines.append("• Visual Studio Code: 未找到原始配置备份，未改动当前配置文件")
                else:
                    tool_lines.append("• Visual Studio Code: 已检测到，可手动检查 settings.json")
            else:
                tool_lines.append("• Visual Studio Code: 未安装，跳过恢复")

            status_summary = self._console_status_summary()
            status_lines = [f"• {part.strip()}" for part in status_summary.split("；") if part.strip()]
            if not status_lines:
                status_lines = ["• 未检测到控制台状态"]

            summary_parts = [
                header,
                "",
                "当前工具配置：",
                *tool_lines,
                "",
                "当前控制台编码：",
                *status_lines,
            ]
            summary = "\n".join(summary_parts)

            self._ui_call(self._on_restore_finished, summary)
        except Exception as exc:  # noqa: BLE001
            self._log(f"恢复失败: {exc}", "error")
            self._ui_call(self._on_restore_failed, str(exc))

    def _on_restore_finished(self, summary: str) -> None:
        self.is_running = False
        self._progress_finish()
        self._set_buttons_state(True)
        self._update_restore_button_state()
        # 恢复后重新检测路径与状态，刷新界面显示（检测分隔线在 _detect_all_paths 内部）
        self._detect_all_paths()
        self._show_modal("完成", summary, kind="info")

    def _on_restore_failed(self, message: str) -> None:
        self.is_running = False
        self._set_buttons_state(True)
        self._update_restore_button_state()
        self._show_modal("错误", f"恢复失败：{message}", kind="error")


def main() -> None:
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Code-encoding-fix")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        main()
    else:
        print("仅支持在 Windows 上运行 tkinter GUI。")

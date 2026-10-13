#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitLab 多目标分支合并请求批量创建工具

功能：选择一个源分支，勾选多个目标分支，一键创建合并请求并统一指派给指定用户。
技术栈：Python 3 + Tkinter（GUI）+ requests（HTTP），无其他第三方依赖。

用法：python3 gitlab_mr_tool.py
"""

import json
import logging
import os
import queue
import threading
from datetime import datetime
from urllib.parse import quote

import tkinter as tk
from tkinter import messagebox, ttk

try:
    import requests
    import urllib3
except ImportError:  # pragma: no cover - 运行环境缺少 requests 时给出明确提示
    requests = None
    urllib3 = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_DIR = os.path.expanduser("~/.gitlab_mr_tool")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")

DEFAULT_CONFIG = {
    "gitlab_url": "https://gitlab.example.com",
    "project_path": "",
    "token": "",
    "proxy": "",
    "verify_ssl": True,
    "timeout": 30,
    "branch_limit": 100,
    "target_branches": ["dev", "qa", "uat", "stg"],
    "last_assignee": "",
    "last_checked_targets": [],
    "config_collapsed": False,
}

# 目标分支远程不存在 / 源分支不存在时的提示语
MSG_SOURCE_NOT_EXIST = "请先 push 分支"


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------

class ConfigManager:
    """负责 ~/.gitlab_mr_tool/config.json 的读取与保存。"""

    def __init__(self, config_file=CONFIG_FILE):
        self.config_file = config_file
        self.config = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        """从磁盘读取配置，缺失或损坏时回退到默认值。"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 用磁盘上的值覆盖默认值，但保留默认结构，避免新增字段缺失
                for key, value in data.items():
                    self.config[key] = value
        except (OSError, ValueError) as exc:
            logging.warning("读取配置文件失败，使用默认配置: %s", exc)

    def save(self):
        """保存配置到磁盘，目录不存在时自动创建。"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return True, ""
        except OSError as exc:
            return False, str(exc)

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value


# ---------------------------------------------------------------------------
# 日志管理
# ---------------------------------------------------------------------------

class AppLogger:
    """按天写入日志文件，绝不记录 Token。

    info()/error() 支持标准 logging 的惰性 % 格式化参数，调用方式与
    logging.getLogger(...).info(...) 一致。
    """

    def __init__(self, log_dir=LOG_DIR):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.logger = logging.getLogger("mr_tool")
        self.logger.setLevel(logging.INFO)
        self._current_date = None
        self._ensure_handler()

    def _ensure_handler(self):
        """仅在日志文件跨天切换时才重建 handler，避免每次写日志都反复开关文件。"""
        today = datetime.now().strftime("%Y%m%d")
        if today == self._current_date:
            return
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)
        log_file = os.path.join(self.log_dir, "mr_create_{}.log".format(today))
        handler = logging.FileHandler(log_file, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self._current_date = today

    def info(self, message, *args, **kwargs):
        self._ensure_handler()
        self.logger.info(message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        self._ensure_handler()
        self.logger.error(message, *args, **kwargs)


# ---------------------------------------------------------------------------
# GitLab API 封装
# ---------------------------------------------------------------------------

class GitLabAPI:
    """封装 GitLab REST API v4 调用，统一处理认证、代理、SSL 与超时。"""

    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers["PRIVATE-TOKEN"] = config.get("token", "")
        # 代理：留空则直连
        proxy = config.get("proxy", "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        # SSL：默认校验，勾选后跳过
        self.session.verify = bool(config.get("verify_ssl", True))
        if not self.session.verify and urllib3 is not None:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.timeout = int(config.get("timeout", 30))

    # ---- 基础请求 ----

    def _url(self, path):
        base = self.config.get("gitlab_url", "").rstrip("/")
        return "{}/api/v4/{}".format(base, path)

    def _request(self, method, path, **kwargs):
        """发送请求并解析 JSON，失败时抛出带说明的异常。"""
        kwargs.setdefault("timeout", self.timeout)
        try:
            resp = self.session.request(method, self._url(path), **kwargs)
        except requests.exceptions.SSLError as exc:
            raise GitLabAPIError("SSL 校验失败，可尝试勾选「跳过 SSL 校验」: {}".format(exc))
        except requests.exceptions.ProxyError as exc:
            raise GitLabAPIError("代理连接失败，请检查代理设置: {}".format(exc))
        except requests.exceptions.ConnectionError as exc:
            raise GitLabAPIError("无法连接 GitLab，请检查地址与网络: {}".format(exc))
        except requests.exceptions.Timeout as exc:
            raise GitLabAPIError("请求超时（{} 秒），请检查网络或调大超时: {}".format(self.timeout, exc))
        except requests.exceptions.RequestException as exc:
            raise GitLabAPIError("网络请求失败: {}".format(exc))

        if resp.status_code == 401:
            raise GitLabAPIError("认证失败，Token 无效或缺少 api 权限")
        if resp.status_code == 403:
            raise GitLabAPIError("权限不足（403），Token 需要 api 权限")
        if resp.status_code == 404:
            raise GitLabAPIError("项目不存在，请检查 GitLab 地址与项目路径")
        if resp.status_code >= 400:
            raise GitLabAPIError("请求失败（HTTP {}）: {}".format(
                resp.status_code, resp.text[:200]))
        try:
            return resp.json()
        except ValueError as exc:
            raise GitLabAPIError("响应不是合法 JSON: {}".format(exc))

    def _project_id(self):
        return quote(self.config.get("project_path", ""), safe="")

    # ---- 业务方法 ----

    def fetch_branches(self, search=None):
        """拉取分支。

        search 不为空时，优先使用 API 的 search 参数；随后在本地按提交时间降序排序。
        """
        params = {"per_page": int(self.config.get("branch_limit", 100))}
        if search:
            params["search"] = search
        data = self._request("GET", "projects/{}/repository/branches".format(
            self._project_id()), params=params)
        branches = [item["name"] for item in data if item.get("name")]
        if search:
            return branches
        # 无 search 时按最近提交时间排序（自建 GitLab 不支持 order_by=updated_at）
        items = sorted(
            data,
            key=lambda b: b.get("commit", {}).get("committed_date", ""),
            reverse=True,
        )
        return [item["name"] for item in items]

    def fetch_members(self):
        """拉取项目全部成员（含继承成员），返回 [(id, name, username)] 列表。"""
        all_members = []
        page = 1
        while True:
            data = self._request(
                "GET", "projects/{}/members/all".format(self._project_id()),
                params={"page": page, "per_page": 100})
            if not data:
                break
            for item in data:
                username = item.get("username") or ""
                name = item.get("name") or username
                all_members.append((item.get("id"), name, username))
            page += 1
        return all_members

    def list_open_mrs(self, source, target):
        """查询同一 源分支 -> 目标分支 已存在的未关闭 MR。"""
        data = self._request(
            "GET", "projects/{}/merge_requests".format(self._project_id()),
            params={"source_branch": source, "target_branch": target,
                    "state": "opened"})
        return data

    def create_mr(self, source, target, assignee_id):
        """创建合并请求，返回创建后的 MR 对象。"""
        payload = {
            "source_branch": source,
            "target_branch": target,
            "title": "Merge {} into {}".format(source, target),
            "description": "",
            "remove_source_branch": False,
        }
        if assignee_id:
            payload["assignee_id"] = assignee_id
        return self._request(
            "POST", "projects/{}/merge_requests".format(self._project_id()),
            json=payload)

    def branch_exists(self, branch_name):
        """用精确端点校验远程分支是否存在（不限分支数量）。

        只有 404 表示分支不存在；网络/认证等错误照常抛出，避免误报。
        """
        url = self._url("projects/{}/repository/branches/{}".format(
            self._project_id(), quote(branch_name, safe="")))
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise GitLabAPIError("校验分支时网络请求失败: {}".format(exc))
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        # 其余状态码复用统一的错误处理逻辑
        self._request("GET", "projects/{}/repository/branches/{}".format(
            self._project_id(), quote(branch_name, safe="")))
        return False


class GitLabAPIError(Exception):
    """带用户可读信息的 API 异常。"""


# ---------------------------------------------------------------------------
# 可搜索下拉框（Entry + 弹出 Listbox）
# ---------------------------------------------------------------------------

# 模块级函数：Ctrl+A 全选
def _select_all_text(event):
    event.widget.select_range(0, tk.END)
    event.widget.icursor(tk.END)
    return "break"


class SearchableCombobox(ttk.Frame):
    """支持本地实时过滤 + 可选远程搜索 + 手动输入的下拉框。

    - on_search: 可选，签名 (text, callback) 的远程搜索回调。
      callback(results) 由调用方在拿到结果后调用，触发 set_search_results。
    """

    def __init__(self, master, values=(), width=40, on_search=None):
        super().__init__(master)
        self._base_values = list(values)   # 全量列表（刷新时设置）
        self.values = list(values)          # 当前工作列表（可能是本地过滤或远程结果）
        self.filtered = list(values)         # 弹出框实际展示的候选
        self.var = tk.StringVar()
        self._popup = None
        self._click_bind = None            # 全局点击绑定的 funcid
        self.on_search = on_search          # 可选：text -> None

        # 布局：文本框在左占满，箭头按钮紧贴文本框右侧
        self.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        # Ctrl+A 全选
        self.entry.bind("<Control-a>", _select_all_text)
        self.entry.bind("<Control-A>", _select_all_text)

        self.arrow = ttk.Button(self, text="▾", width=2, command=self._toggle)
        self.arrow.grid(row=0, column=1, sticky="e")

    # ---- 公开接口 ----

    def get(self):
        return self.var.get()

    def set(self, value):
        self.var.set(value or "")

    def set_values(self, values):
        """更新全量列表（刷新分支/成员时调用）。"""
        self._base_values = list(values)
        self.values = list(values)
        self.filtered = list(values)
        self._rebuild_popup()

    def set_search_results(self, values):
        """应用远程搜索结果，立即显示弹出框。"""
        self.values = list(values)
        self.filtered = list(values)
        self._show_popup()

    def show_popup(self):
        """主动打开弹出框（用于远程搜索结果应用后）。"""
        if self.filtered:
            self._show_popup()

    # ---- 内部实现 ----

    def _on_key(self, event):
        text = self.var.get().strip()
        if text:
            self.filtered = [v for v in self.values
                             if text.lower() in str(v).lower()]
        else:
            # 清空输入框时恢复全量列表
            self.values = list(self._base_values)
            self.filtered = list(self.values)
        if event.keysym in ("Down", "Up", "Return", "Escape"):
            return
        self._show_popup()
        # 有远程搜索回调且输入非空时触发（防抖由调用方负责）
        if self.on_search and text:
            self.on_search(text)

    def _on_focus_out(self, event):
        # 焦点离开输入框时延迟关闭弹出框（全局点击关闭是主要防线，这里是兜底）
        self.after(200, self._check_hide)

    def _check_hide(self):
        if not self._popup or not self._popup.winfo_exists():
            return
        try:
            root = self.winfo_toplevel()
            x = root.winfo_pointerx()
            y = root.winfo_pointery()
            widget_under = root.winfo_containing(x, y)
            over_popup = (widget_under is not None and
                          (widget_under is self._popup or
                           widget_under is self._popup.listbox))
            over_self = widget_under in (self.entry, self.arrow)
            if over_popup or over_self:
                return
        except tk.TclError:
            pass
        self._hide_popup()

    def _on_global_click(self, event):
        """全局点击回调：点击弹出框和输入框之外时关闭弹出框。"""
        widget = event.widget
        if widget is self.entry or widget is self.arrow:
            return
        if self._popup and widget is self._popup.listbox:
            return
        self._hide_popup()

    def _toggle(self):
        if self._popup and self._popup.winfo_exists():
            self._hide_popup()
        else:
            self.filtered = list(self.values)
            self._show_popup()

    def _show_popup(self):
        if self._popup and self._popup.winfo_exists():
            self._rebuild_popup()
            return
        height = min(12, max(1, len(self.filtered)))
        self._popup = tk.Toplevel(self)
        self._popup.overrideredirect(True)
        self._popup.attributes("-topmost", True)
        listbox = tk.Listbox(self._popup, height=height)
        listbox.pack(fill="both", expand=True)
        self._popup.listbox = listbox
        listbox.bind("<Button-1>", self._on_select)
        self._place_popup()
        self._rebuild_popup()
        # 注册全局点击：点击输入框与弹出框以外区域时自动关闭
        self._click_bind = self.bind_all("<Button-1>", self._on_global_click)

    def _place_popup(self):
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        w = self.entry.winfo_width()
        self._popup.geometry("{}x{}+{}+{}".format(w, 200, x, y))

    def _rebuild_popup(self):
        if not self._popup or not self._popup.winfo_exists():
            return
        listbox = self._popup.listbox
        listbox.delete(0, tk.END)
        for value in self.filtered:
            listbox.insert(tk.END, value)
        listbox.config(height=min(12, max(1, len(self.filtered))))

    def _on_select(self, event):
        listbox = getattr(self._popup, "listbox", None)
        if listbox:
            index = listbox.nearest(event.y)
            if index >= 0:
                self.var.set(listbox.get(index))
        self._hide_popup()

    def _hide_popup(self):
        if self._click_bind:
            try:
                self.unbind_all("<Button-1>")
            except tk.TclError:
                pass
            self._click_bind = None
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
        self._popup = None


# ---------------------------------------------------------------------------
# 预览弹窗
# ---------------------------------------------------------------------------

class PreviewDialog(tk.Toplevel):
    """预览将创建的 MR 表格，确认后返回。"""

    def __init__(self, master, rows):
        # rows: [(source, target, assignee, title)]
        super().__init__(master)
        self.title("预览将创建的 MR")
        self.transient(master)
        self.grab_set()
        self.confirmed = False

        columns = ("source", "target", "assignee", "title")
        headings = ("源分支", "目标分支", "指派人", "标题")
        tree = ttk.Treeview(self, columns=columns, show="headings", height=len(rows))
        for col, heading in zip(columns, headings):
            tree.heading(col, text=heading)
            tree.column(col, width=180, anchor="w")
        for row in rows:
            tree.insert("", tk.END, values=row)
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(btn_frame, text="确认创建", command=self._confirm).pack(side="right", padx=5)

    def _confirm(self):
        self.confirmed = True
        self.destroy()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class Application(tk.Tk):
    """主窗口，按需求文档组织五块区域。"""

    def __init__(self):
        super().__init__()
        self.title("GitLab 批量创建 MR 工具")
        self.geometry("860x720")

        # Ctrl+A 全选：对所有 Entry / TEntry 生效
        self.bind_class("Entry", "<Control-a>", _select_all_text)
        self.bind_class("Entry", "<Control-A>", _select_all_text)
        self.bind_class("TEntry", "<Control-a>", _select_all_text)
        self.bind_class("TEntry", "<Control-A>", _select_all_text)

        self.config_manager = ConfigManager()
        self.logger = AppLogger()
        self.logger.info("======== 工具启动 ========")
        self.api = None  # 延迟到配置变化时重建
        self.branches = []
        self.members = []  # [(id, name, username)]

        # 远程搜索防抖
        self._search_after_id = None

        # 后台任务队列，worker 线程把结果投递到这里，由主线程轮询更新 UI
        self.task_queue = queue.Queue()

        self._build_ui()
        self._load_config_to_ui()
        self._rebuild_api()
        # 应用配置区收起/展开状态
        if self.config_manager.get("config_collapsed", False):
            self.cfg_frame.pack_forget()
            self.toggle_cfg_btn.configure(text="展开配置 ▼")
            self._config_visible = False
        # 关闭窗口时自动保存指派人、勾选状态等
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)
        # 启动后自动刷新分支与成员（有 Token + 项目路径时）
        if (self.api and
                self.config_manager.get("project_path", "").strip() and
                self.config_manager.get("token", "").strip()):
            self.after(200, lambda: self._refresh_branches(silent=True))
            self.after(400, lambda: self._refresh_members(silent=True))

    # ---- UI 构建 ----

    def _build_ui(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        # 1. 配置区（可收起）
        cfg_bar = ttk.Frame(main)
        cfg_bar.pack(fill="x")
        self._config_visible = True
        self.toggle_cfg_btn = ttk.Button(cfg_bar, text="收起配置 ▲",
                                         command=self._toggle_config)
        self.toggle_cfg_btn.pack(side="left")

        self.cfg_frame = ttk.LabelFrame(main, text="配置", padding=8)
        self.cfg_frame.pack(fill="x")

        cfg_frame = self.cfg_frame
        cfg_frame.columnconfigure(1, weight=1)
        self.gitlab_url_var = tk.StringVar()
        self.project_path_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.proxy_var = tk.StringVar()
        self.show_token_var = tk.BooleanVar(value=False)
        self.verify_ssl_var = tk.BooleanVar(value=True)

        self._add_row(cfg_frame, 0, "GitLab 地址", self.gitlab_url_var)
        self._add_row(cfg_frame, 1, "项目路径", self.project_path_var,
                      hint="格式：group/subgroup/project")

        ttk.Label(cfg_frame, text="Token").grid(row=2, column=0, sticky="w", pady=2)
        token_entry = ttk.Entry(cfg_frame, textvariable=self.token_var, show="*")
        token_entry.grid(row=2, column=1, sticky="ew", pady=2)
        self.token_entry = token_entry

        ttk.Label(cfg_frame, text="代理地址").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(cfg_frame, textvariable=self.proxy_var).grid(
            row=3, column=1, sticky="ew", pady=2)

        bottom = ttk.Frame(cfg_frame)
        bottom.grid(row=4, column=1, sticky="w", pady=2)
        ttk.Checkbutton(bottom, text="显示 Token", variable=self.show_token_var,
                        command=self._toggle_token_visible).pack(side="left")
        ttk.Checkbutton(bottom, text="跳过 SSL 校验", variable=self.verify_ssl_var).pack(
            side="left", padx=10)
        ttk.Button(bottom, text="保存配置", command=self._save_config).pack(side="left", padx=10)

        # 2. 源分支区
        self.src_frame = ttk.LabelFrame(main, text="源分支", padding=8)
        self.src_frame.pack(fill="x", pady=(8, 0))
        self.src_frame.columnconfigure(1, weight=1)
        ttk.Label(self.src_frame, text="源分支").grid(row=0, column=0, sticky="w", pady=2)
        self.source_combo = SearchableCombobox(self.src_frame, width=60,
                                              on_search=self._on_source_search_typed)
        self.source_combo.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Button(self.src_frame, text="刷新分支列表",
                   command=self._refresh_branches).grid(row=0, column=2, padx=5)

        # 3. 目标分支区
        target_frame = ttk.LabelFrame(main, text="目标分支", padding=8)
        target_frame.pack(fill="x", pady=(8, 0))
        self.target_check_frame = ttk.Frame(target_frame)
        self.target_check_frame.pack(side="left", fill="x", expand=True)
        ttk.Button(target_frame, text="编辑目标分支",
                   command=self._edit_targets).pack(side="right")

        # 4. 指派人区
        assignee_frame = ttk.LabelFrame(main, text="指派给", padding=8)
        assignee_frame.pack(fill="x", pady=(8, 0))
        assignee_frame.columnconfigure(1, weight=1)
        ttk.Label(assignee_frame, text="指派给").grid(row=0, column=0, sticky="w", pady=2)
        self.assignee_combo = SearchableCombobox(assignee_frame, width=60)
        self.assignee_combo.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Button(assignee_frame, text="刷新成员列表",
                   command=self._refresh_members).grid(row=0, column=2, padx=5)

        # 5. 操作区
        action_frame = ttk.LabelFrame(main, text="操作", padding=8)
        action_frame.pack(fill="both", expand=True, pady=(8, 0))
        btn_row = ttk.Frame(action_frame)
        btn_row.pack(fill="x")
        self.preview_btn = ttk.Button(btn_row, text="预览将创建的 MR",
                                      command=self._preview)
        self.preview_btn.pack(side="left")
        self.create_btn = ttk.Button(btn_row, text="确认创建",
                                     command=self._create, state="disabled")
        self.create_btn.pack(side="left", padx=5)

        self.result_text = tk.Text(action_frame, height=8, state="disabled")
        result_scroll = ttk.Scrollbar(action_frame, command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=result_scroll.set)
        self.result_text.pack(fill="both", expand=True, pady=(8, 0), side="left")
        result_scroll.pack(fill="y", side="right", pady=(8, 0))
        self.result_text.tag_configure("success", foreground="#1a7f37")
        self.result_text.tag_configure("skip", foreground="#9a6700")
        self.result_text.tag_configure("fail", foreground="#cf222e")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var).pack(anchor="w", pady=(6, 0))

    def _add_row(self, parent, row, label, var, hint=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        if hint:
            ttk.Label(parent, text=hint, foreground="gray").grid(
                row=row, column=2, sticky="w", padx=5)

    # ---- 配置与 UI 同步 ----

    def _load_config_to_ui(self):
        cfg = self.config_manager.config
        self.gitlab_url_var.set(cfg.get("gitlab_url", ""))
        self.project_path_var.set(cfg.get("project_path", ""))
        self.token_var.set(cfg.get("token", ""))
        self.proxy_var.set(cfg.get("proxy", ""))
        self.verify_ssl_var.set(bool(cfg.get("verify_ssl", True)))
        self.assignee_combo.set(cfg.get("last_assignee", ""))
        self._rebuild_target_checkboxes(cfg.get("target_branches", []),
                                        set(cfg.get("last_checked_targets", [])))

    def _collect_config(self):
        cfg = self.config_manager.config
        cfg["gitlab_url"] = self.gitlab_url_var.get().strip()
        cfg["project_path"] = self.project_path_var.get().strip()
        cfg["token"] = self.token_var.get().strip()
        cfg["proxy"] = self.proxy_var.get().strip()
        cfg["verify_ssl"] = self.verify_ssl_var.get()
        return cfg

    def _save_config(self):
        cfg = self._collect_config()
        self.config_manager.config.update(cfg)
        # 记住当前勾选与指派人
        self.config_manager.set("last_assignee", self.assignee_combo.get())
        self.config_manager.set("last_checked_targets", self._checked_targets())
        ok, err = self.config_manager.save()
        if ok:
            self._rebuild_api()
            self._set_status("配置已保存")
        else:
            messagebox.showerror("保存失败", "无法保存配置：{}".format(err))

    def _rebuild_api(self):
        cfg = self.config_manager.config
        if requests is None:
            self.api = None
            messagebox.showerror(
                "缺少依赖", "未安装 requests 库，请先执行：pip3 install requests")
            return
        self.api = GitLabAPI(cfg)

    def _toggle_config(self):
        """收起/展开顶部配置区。"""
        if self._config_visible:
            self.cfg_frame.pack_forget()
            self.toggle_cfg_btn.configure(text="展开配置 ▼")
            self._config_visible = False
        else:
            # before 指定插入位置，避免 pack 追加到末尾
            self.cfg_frame.pack(before=self.src_frame, fill="x")
            self.toggle_cfg_btn.configure(text="收起配置 ▲")
            self._config_visible = True
        self.config_manager.set("config_collapsed", not self._config_visible)
        self.config_manager.save()

    def _toggle_token_visible(self):
        self.token_entry.configure(show="" if self.show_token_var.get() else "*")

    # ---- 目标分支复选框 ----

    def _rebuild_target_checkboxes(self, branches, checked):
        for child in self.target_check_frame.winfo_children():
            child.destroy()
        self.target_vars = {}
        for branch in branches:
            var = tk.BooleanVar(value=branch in checked)
            self.target_vars[branch] = var
            ttk.Checkbutton(self.target_check_frame, text=branch,
                            variable=var).pack(side="left", padx=5, pady=2)

    def _checked_targets(self):
        return [b for b, var in self.target_vars.items() if var.get()]

    def _edit_targets(self):
        dialog = EditTargetsDialog(self, self.config_manager.config["target_branches"])
        self.wait_window(dialog)
        if dialog.result is not None:
            self.config_manager.set("target_branches", dialog.result)
            self.config_manager.save()
            self._rebuild_target_checkboxes(dialog.result, set(self._checked_targets()))

    # ---- 数据刷新 ----

    def _refresh_branches(self, silent=False):
        if not self.api:
            return
        self._refresh_silent = silent
        self._set_status("正在加载分支...")
        threading.Thread(target=self._fetch_branches_worker, daemon=True).start()

    def _fetch_branches_worker(self):
        try:
            branches = self.api.fetch_branches()
            self.task_queue.put(("branches", branches, None))
        except GitLabAPIError as exc:
            self.task_queue.put(("branches", None, str(exc)))

    def _refresh_members(self, silent=False):
        if not self.api:
            return
        self._refresh_silent = silent
        self._set_status("正在加载成员...")
        threading.Thread(target=self._fetch_members_worker, daemon=True).start()

    def _fetch_members_worker(self):
        try:
            members = self.api.fetch_members()
            self.task_queue.put(("members", members, None))
        except GitLabAPIError as exc:
            self.task_queue.put(("members", None, str(exc)))

    def _on_branches_loaded(self, branches, error):
        silent = getattr(self, "_refresh_silent", False)
        if error:
            if not silent:
                messagebox.showerror("加载分支失败", error)
            self._set_status("分支加载失败")
            self.logger.error("加载分支失败: %s", error)
            return
        self.branches = branches
        self.source_combo.set_values(branches)
        self._set_status("已加载 {} 个分支".format(len(branches)))
        self.logger.info("加载分支完成: %d 个", len(branches))

    def _on_members_loaded(self, members, error):
        silent = getattr(self, "_refresh_silent", False)
        if error:
            if not silent:
                messagebox.showerror("加载成员失败", error)
            self._set_status("成员加载失败")
            self.logger.error("加载成员失败: %s", error)
            return
        self.members = members
        labels = ["{} ({})".format(name, username) for _, name, username in members]
        self.assignee_combo.set_values(labels)
        self._set_status("已加载 {} 名成员".format(len(members)))
        self.logger.info("加载成员完成: %d 名", len(members))

    # ---- 远程分支搜索（防抖） ----

    def _on_source_search_typed(self, text):
        """用户在源分支下拉框输入时触发，400ms 防抖后启动 API 搜索。"""
        if not self.api or not text.strip():
            return
        if self._search_after_id:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(400, lambda: self._start_branch_search(text))

    def _start_branch_search(self, text):
        threading.Thread(target=self._branch_search_worker, args=(text,),
                         daemon=True).start()

    def _branch_search_worker(self, text):
        try:
            branches = self.api.fetch_branches(search=text)
            self.task_queue.put(("search", (text, branches), None))
        except GitLabAPIError as exc:
            self.task_queue.put(("search", (text, None), str(exc)))

    def _on_search_result(self, text, branches, error):
        """应用远程分支搜索结果，仅当当前输入一致时才更新（避免过期结果覆盖）。"""
        current = self.source_combo.get().strip()
        if text.strip() != current:
            return  # 过期结果，丢弃
        if error:
            # 搜索失败不弹窗，保留本地过滤结果
            self.logger.error("分支搜索 %s 失败: %s", text, error)
            return
        if branches is not None:
            self.source_combo.set_search_results(branches)

    # ---- 预览与创建 ----

    def _validate_source(self, source):
        if not source:
            raise GitLabAPIError("请填写源分支")
        # 用精确端点校验，不依赖缓存的分支列表（列表只包含最近 N 个）
        if not self.api.branch_exists(source):
            raise GitLabAPIError(MSG_SOURCE_NOT_EXIST)

    def _validate_targets(self, targets):
        if not targets:
            raise GitLabAPIError("请至少勾选一个目标分支")
        missing = []
        for target in targets:
            if not self.api.branch_exists(target):
                missing.append(target)
        if missing:
            raise GitLabAPIError(
                "目标分支 {} 在远程不存在，请修正后再试".format(", ".join(missing)))

    def _resolve_assignee(self, label):
        if not label:
            return None, "未指定"
        for member_id, name, username in self.members:
            if label == "{} ({})".format(name, username):
                return member_id, name
        raise GitLabAPIError("指派人不是项目成员，请重新选择")

    def _preview(self):
        try:
            source = self.source_combo.get().strip()
            targets = self._checked_targets()
            self._set_status("正在校验分支...")
            self._validate_source(source)
            self._validate_targets(targets)
            _, assignee_name = self._resolve_assignee(self.assignee_combo.get().strip())
        except GitLabAPIError as exc:
            messagebox.showerror("无法预览", str(exc))
            self._set_status("校验未通过")
            return

        self.logger.info("预览 MR：源分支=%s，目标分支=%s，指派人=%s",
                         source, ",".join(targets), assignee_name)
        rows = [(source, target, assignee_name,
                 "Merge {} into {}".format(source, target)) for target in targets]
        dialog = PreviewDialog(self, rows)
        self.wait_window(dialog)
        if dialog.confirmed:
            self._do_create(source, targets)

    def _create(self):
        # 预览确认后才会启用创建按钮；这里保留入口以便复用
        source = self.source_combo.get().strip()
        targets = self._checked_targets()
        try:
            self._validate_source(source)
            self._validate_targets(targets)
        except GitLabAPIError as exc:
            messagebox.showerror("无法创建", str(exc))
            return
        self._do_create(source, targets)

    def _do_create(self, source, targets):
        try:
            assignee_id, assignee_name = self._resolve_assignee(
                self.assignee_combo.get().strip())
        except GitLabAPIError as exc:
            messagebox.showerror("无法创建", str(exc))
            return
        # 记住当前选择以便下次打开自动恢复
        self._persist_selections()
        self.create_btn.configure(state="disabled")
        self.preview_btn.configure(state="disabled")
        self._set_status("正在创建 MR...")
        self._clear_result()
        self.logger.info("开始创建 MR：源=%s，目标=%s，指派人=%s",
                         source, ",".join(targets), assignee_name)
        threading.Thread(target=self._create_worker,
                         args=(source, targets, assignee_id), daemon=True).start()

    def _create_worker(self, source, targets, assignee_id):
        results = []
        for target in targets:
            try:
                existing = self.api.list_open_mrs(source, target)
                if existing:
                    url = existing[0].get("web_url", "")
                    results.append(("skip", target, url,
                                    "已存在，已跳过：{}".format(url)))
                    self.logger.info("跳过 MR %s -> %s: %s", source, target, url)
                    continue
                mr = self.api.create_mr(source, target, assignee_id)
                url = mr.get("web_url", "")
                iid = mr.get("iid", "")
                results.append(("success", target, url,
                                "创建成功：{}".format(url)))
                self.logger.info("创建 MR !%s %s -> %s 成功: %s",
                                 iid, source, target, url)
            except GitLabAPIError as exc:
                results.append(("fail", target, "", "创建失败：{}".format(exc)))
                self.logger.error("创建 MR %s -> %s 失败: %s", source, target, exc)
            except Exception as exc:
                # 防御性捕获：日志/队列/线程相关的意外错误不会中断整个批量
                results.append(("fail", target, "",
                                "内部错误：{}".format(exc)))
                self.logger.error("创建 MR %s -> %s 内部错误: %s",
                                  source, target, exc, exc_info=True)
        self.task_queue.put(("created", results, None))

    def _on_created(self, results, _error):
        self.create_btn.configure(state="normal")
        self.preview_btn.configure(state="normal")
        for status, target, url, text in results:
            if status == "success":
                self._append_result("{} -> 目标分支 {}：{}".format(
                    "成功", target, text), "success")
            elif status == "skip":
                self._append_result("{} -> 目标分支 {}：{}".format(
                    "跳过", target, text), "skip")
            else:
                self._append_result("{} -> 目标分支 {}：{}".format(
                    "失败", target, text), "fail")
        self._set_status("创建完成，共 {} 个目标分支".format(len(results)))

    # ---- 队列轮询与结果展示 ----

    def _poll_queue(self):
        try:
            while True:
                event, payload, error = self.task_queue.get_nowait()
                if event == "branches":
                    self._on_branches_loaded(payload, error)
                elif event == "members":
                    self._on_members_loaded(payload, error)
                elif event == "search":
                    text, branches = payload
                    self._on_search_result(text, branches, error)
                elif event == "created":
                    self._on_created(payload, error)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _append_result(self, text, tag):
        self.result_text.configure(state="normal")
        self.result_text.insert(tk.END, text + "\n", tag)
        self.result_text.see(tk.END)
        self.result_text.configure(state="disabled")

    def _clear_result(self):
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", tk.END)
        self.result_text.configure(state="disabled")

    def _set_status(self, text):
        self.status_var.set(text)

    def _persist_selections(self):
        """将当前指派人选择和勾选状态写入配置文件。"""
        try:
            self.config_manager.set("last_assignee", self.assignee_combo.get())
            self.config_manager.set("last_checked_targets", self._checked_targets())
            self.config_manager.save()
        except Exception:
            pass  # 保存失败不影响正常使用

    def _on_close(self):
        """关闭窗口前持久化当前选择。"""
        self._persist_selections()
        self.logger.info("======== 工具关闭 ========")
        self.destroy()


# ---------------------------------------------------------------------------
# 目标分支编辑弹窗
# ---------------------------------------------------------------------------

class EditTargetsDialog(tk.Toplevel):
    """增删改目标分支列表。"""

    def __init__(self, master, branches):
        super().__init__(master)
        self.title("编辑目标分支")
        self.transient(master)
        self.grab_set()
        self.result = None

        self.branches = list(branches)

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(frame, height=10, width=40)
        self.listbox.pack(fill="both", expand=True)
        for branch in self.branches:
            self.listbox.insert(tk.END, branch)

        self.entry_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.entry_var).pack(fill="x", pady=5)

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="添加", command=self._add).pack(side="left")
        ttk.Button(btn_row, text="删除选中", command=self._remove).pack(side="left", padx=5)
        ttk.Button(btn_row, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text="保存", command=self._save).pack(side="right", padx=5)

    def _add(self):
        name = self.entry_var.get().strip()
        if not name:
            return
        if name not in self.branches:
            self.branches.append(name)
            self.listbox.insert(tk.END, name)
            self.entry_var.set("")

    def _remove(self):
        selection = self.listbox.curselection()
        if selection:
            index = selection[0]
            self.branches.pop(index)
            self.listbox.delete(index)

    def _save(self):
        self.result = [b for b in self.branches if b]
        self.destroy()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    if requests is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "缺少依赖", "未安装 requests 库，请先执行：pip3 install requests")
        return
    app = Application()
    app.mainloop()


if __name__ == "__main__":
    main()

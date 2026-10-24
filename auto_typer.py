# -*- coding: utf-8 -*-
"""
全局热键自动输入小工具
=======================

功能：
    1. tkinter 作为主界面（主线程运行 mainloop）
    2. pynput.GlobalHotKeys 在子线程中监听全局热键 Ctrl+Shift+S
    3. 热键触发后读取文本框内容，按设定的间隔逐字符模拟键盘输入
    4. threading.Lock 做防并发：打字过程中再次触发会被忽略
    5. 支持调节「字符间隔」与「开始前延迟」

依赖安装：
    pip install pynput

运行：
    python auto_typer.py

注意：
    * Linux 需要 X11 环境（Wayland 下 pynput 可能无法监听全局热键）
    * 中文等非 ASCII 字符能否输入取决于目标窗口/系统，
      pynput 在 Windows 上通过 KEYEVENTF_UNICODE 发送，多数场景可用；
      若目标程序不接收，可改用「剪贴板 + Ctrl+V」方案。
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext

from pynput import keyboard


class AutoTyperApp:
    """主应用类：GUI + 热键监听 + 模拟输入"""

    # pynput 的热键语法：<ctrl>+<shift>+s
    HOTKEY = "<ctrl>+<shift>+s"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("全局热键自动输入（Ctrl+Shift+S 触发）")
        self.root.geometry("600x500")
        self.root.minsize(480, 400)

        # ---------------- 线程间通信队列 ----------------
        # pynput 监听线程、打字线程都不能直接操作 tkinter 控件（非线程安全），
        # 统一往队列里投递消息，由主线程定时取出处理。
        self._queue = queue.Queue()

        # ---------------- 并发控制 ----------------
        # 防并发锁：同一时刻只允许一个打字任务。
        # 用 acquire(blocking=False) 实现「拿不到就立即放弃」，不会阻塞。
        self._typing_lock = threading.Lock()

        # 停止信号：点击「停止」按钮时置位，打字循环检测到后退出
        self._stop_event = threading.Event()

        # 热键监听器对象（关闭窗口时需要 stop）
        self._hotkey_listener = None

        self._build_ui()
        self._start_hotkey_listener()

        # 主线程每 30ms 轮询一次队列，处理来自子线程的消息
        self.root.after(30, self._poll_queue)

        # 关闭窗口时清理资源
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ==================================================================
    # 一、界面构建
    # ==================================================================
    def _build_ui(self):
        # ---------- 参数设置区 ----------
        param_frame = ttk.LabelFrame(self.root, text="参数设置")
        param_frame.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(param_frame, text="字符间隔（秒）：").grid(
            row=0, column=0, padx=(10, 4), pady=8, sticky="e")
        self.interval_var = tk.StringVar(value="0.05")
        ttk.Spinbox(
            param_frame, from_=0.0, to=5.0, increment=0.01,
            width=8, textvariable=self.interval_var
        ).grid(row=0, column=1, padx=(0, 16), pady=8)

        ttk.Label(param_frame, text="开始前延迟（秒）：").grid(
            row=0, column=2, padx=(10, 4), pady=8, sticky="e")
        self.delay_var = tk.StringVar(value="2")
        ttk.Spinbox(
            param_frame, from_=0, to=30, increment=1,
            width=8, textvariable=self.delay_var
        ).grid(row=0, column=3, padx=(0, 10), pady=8)

        ttk.Label(
            param_frame,
            text="（开始前延迟用于切换到目标窗口）",
            foreground="#888"
        ).grid(row=1, column=0, columnspan=4, padx=10, pady=(0, 8), sticky="w")

        # ---------- 文本框 ----------
        text_frame = ttk.LabelFrame(self.root, text="待输入内容")
        text_frame.pack(fill="both", expand=True, padx=10, pady=6)

        self.text_area = scrolledtext.ScrolledText(
            text_frame, wrap="word", height=12, font=("Consolas", 11)
        )
        self.text_area.pack(fill="both", expand=True, padx=6, pady=6)
        self.text_area.insert(
            "1.0",
            "这里是待输入的文本。\n"
            "把光标放到目标窗口，按 Ctrl+Shift+S 即可开始自动输入。\n"
        )
        # 实时统计字符数
        self.text_area.bind("<KeyRelease>", self._update_char_count)

        # ---------- 按钮区 ----------
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=6)

        ttk.Button(btn_frame, text="开始输入", command=self._start_typing).pack(side="left")
        ttk.Button(btn_frame, text="停止", command=self._on_stop_clicked).pack(side="left", padx=8)
        ttk.Button(
            btn_frame, text="清空",
            command=lambda: self.text_area.delete("1.0", "end")
        ).pack(side="left")

        self.count_label = ttk.Label(btn_frame, text="字符数：0")
        self.count_label.pack(side="right")

        # ---------- 状态栏 ----------
        self.status_var = tk.StringVar(value="就绪：按 Ctrl+Shift+S 触发输入")
        ttk.Label(
            self.root, textvariable=self.status_var,
            anchor="w", relief="sunken", padding=(8, 4)
        ).pack(fill="x", padx=10, pady=(0, 10))

    def _update_char_count(self, _event=None):
        """统计文本框字符数（不含末尾自动换行）"""
        content = self.text_area.get("1.0", "end-1c")
        self.count_label.config(text=f"字符数：{len(content)}")

    # ==================================================================
    # 二、全局热键监听
    # ==================================================================
    def _start_hotkey_listener(self):
        """启动全局热键监听（pynput 内部会自己开一个守护线程）"""
        try:
            self._hotkey_listener = keyboard.GlobalHotKeys(
                {self.HOTKEY: self._on_hotkey}
            )
            self._hotkey_listener.start()
        except Exception as exc:  # 例如无显示环境、无权限等
            self._hotkey_listener = None
            self.status_var.set(f"⚠ 全局热键注册失败：{exc}")

    def _on_hotkey(self):
        """
        【重要】本方法运行在 pynput 的监听线程中！
        绝对不能在这里直接读写 tkinter 控件（会崩溃或死锁），
        所以只做两件事：判断是否在打字中 + 投递消息给主线程。
        """
        # 第一道防并发：正在打字时直接忽略本次触发
        if self._typing_lock.locked():
            return
        # 交给主线程去读取文本框、启动打字线程
        self._queue.put(("hotkey", None))

    # ==================================================================
    # 三、主线程轮询队列
    # ==================================================================
    def _poll_queue(self):
        """
        由主线程周期性调用，处理子线程投递过来的消息。
        这是「子线程 → GUI」唯一安全的数据通道。
        """
        try:
            while True:
                kind, payload = self._queue.get_nowait()

                if kind == "hotkey":
                    # 热键触发 → 在主线程里安全地读取文本框并启动任务
                    self._start_typing()
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "count":
                    self.count_label.config(text=payload)

        except queue.Empty:
            pass

        self.root.after(30, self._poll_queue)

    # ==================================================================
    # 四、启动 / 停止打字任务
    # ==================================================================
    def _start_typing(self):
        """
        尝试启动一次打字任务。
        必须在主线程中调用（因为要读取 tkinter 控件）。
        """
        # 第二道防并发：非阻塞抢锁，抢不到说明已有任务在跑
        if not self._typing_lock.acquire(blocking=False):
            self.status_var.set("⚠ 正在输入中，已忽略本次触发")
            return

        # 读取文本框内容（去掉 tkinter 自动追加的结尾换行）
        text = self.text_area.get("1.0", "end-1c")
        if not text:
            self._typing_lock.release()
            self.status_var.set("文本框为空，无需输入")
            return

        # 解析参数，任何异常都要记得释放锁
        try:
            interval = max(0.0, float(self.interval_var.get()))
            delay = max(0.0, float(self.delay_var.get()))
        except ValueError:
            self._typing_lock.release()
            self.status_var.set("⚠ 参数格式错误：间隔/延迟必须是数字")
            return

        # 清除上一次的停止标记
        self._stop_event.clear()
        self.status_var.set("已触发，准备输入……")

        # 打字放在独立子线程，避免阻塞 GUI
        threading.Thread(
            target=self._type_worker,
            args=(text, interval, delay),
            daemon=True,
        ).start()

    def _on_stop_clicked(self):
        """点击停止按钮：置位停止事件，打字循环下一轮就会退出"""
        if self._typing_lock.locked():
            self._stop_event.set()
            self.status_var.set("正在停止……")
        else:
            self.status_var.set("当前没有进行中的输入任务")

    # ==================================================================
    # 五、打字工作线程
    # ==================================================================
    def _type_worker(self, text, interval, delay):
        """
        在子线程中逐字符模拟键盘输入。
        注意：此方法内不能操作任何 tkinter 控件，只能通过队列回传状态。
        """
        controller = keyboard.Controller()
        total = len(text)
        sent = 0

        try:
            # ---- 开始前延迟：给用户时间切到目标窗口 ----
            if delay > 0:
                # wait() 在期间被 set() 会立即返回 True
                if self._stop_event.wait(delay):
                    self._queue.put(("status", "⏹ 已在延迟阶段取消"))
                    return

            self._queue.put(("status", f"开始输入，共 {total} 个字符……"))

            # ---- 逐字符输入 ----
            for index, ch in enumerate(text, start=1):
                if self._stop_event.is_set():
                    break

                self._send_char(controller, ch)
                sent = index

                # 每 10 个字符刷新一次状态，避免消息刷屏拖慢界面
                if index % 10 == 0 or index == total:
                    self._queue.put(("status", f"输入中…… {index}/{total}"))

                if interval > 0:
                    time.sleep(interval)

            # ---- 结果回传 ----
            if self._stop_event.is_set():
                self._queue.put(("status", f"⏹ 已停止，已输入 {sent}/{total} 个字符"))
            else:
                self._queue.put(("status", f"✅ 输入完成，共 {total} 个字符"))

        except Exception as exc:  # 兜底，防止锁泄漏
            self._queue.put(("status", f"⚠ 输入出错：{exc}"))

        finally:
            # 无论成功失败，最终都必须释放防并发锁
            self._typing_lock.release()

    @staticmethod
    def _send_char(controller: keyboard.Controller, ch: str):
        """把单个字符转换成按键动作发送出去"""
        if ch == "\n":
            # 换行 → 回车键
            controller.press(keyboard.Key.enter)
            controller.release(keyboard.Key.enter)
        elif ch == "\r":
            # Windows 换行可能是 \r\n，跳过 \r 避免多按一次回车
            pass
        elif ch == "\t":
            controller.press(keyboard.Key.tab)
            controller.release(keyboard.Key.tab)
        else:
            # 普通字符：controller.type() 会按当前键盘布局发送
            controller.type(ch)

    # ==================================================================
    # 六、退出清理
    # ==================================================================
    def _on_close(self):
        """关闭窗口：停止打字线程 + 停止热键监听 + 销毁窗口"""
        self._stop_event.set()

        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass

        self.root.destroy()


def main():
    root = tk.Tk()
    # 用 ttk 主题让界面稍微好看一点
    try:
        ttk.Style().theme_use("vista")   # Windows 下可用；其它系统会自动回落
    except tk.TclError:
        pass

    app = AutoTyperApp(root)   # noqa: F841  保留引用，避免被回收
    root.mainloop()


if __name__ == "__main__":
    main()
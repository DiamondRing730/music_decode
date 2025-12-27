import os
import sys
import time
import json
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk


# ================= 配置管理器 =================
class ConfigManager:
    def __init__(self):
        self.config_file = "um_config.json"
        self.default_config = {
            "um_path": "",
            "input_dir": "",
            "output_dir": "",
            "auto_meta": True  # 默认开启元数据补全
        }
        self.config = self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return self.default_config
        return self.default_config

    def save_config(self, key, value):
        self.config[key] = value
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4)

    def get(self, key):
        return self.config.get(key, "")


# ================= 后端逻辑 (Subprocess 调用) =================
class BackendLogic:
    def __init__(self, logger_func):
        self.log = logger_func
        self.running = False
        self.process = None

    def run_um_process(self, um_path, input_dir, output_dir, update_metadata):
        if not os.path.exists(um_path):
            self.log("❌ 错误：未找到 um.exe，请在左侧设置中选择正确的路径！")
            return

        if not os.path.exists(input_dir):
            self.log("❌ 错误：输入目录不存在")
            return

        self.running = True
        try:
            os.makedirs(output_dir, exist_ok=True)

            # 构建命令
            # 参考: um [-o output] [-i] input --update-metadata
            cmd = [
                um_path,
                '-i', input_dir,
                '-o', output_dir,
                '--overwrite',  # 覆盖已存在文件，防止卡住询问
                '--rs',  # remove source: 移除源文件 (可选，这里我没加，安全起见保留源文件)
            ]

            if update_metadata:
                cmd.append('--update-metadata')

            self.log(f"🚀 开始执行任务...")
            self.log(f"📂 输入: {input_dir}")
            self.log(f"📂 输出: {output_dir}")
            if update_metadata:
                self.log("🌐 已启用网络元数据补全 (Cover & Tags)")

            # Windows 下隐藏控制台窗口
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            # 启动子进程
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 将错误输出也重定向到标准输出
                text=True,
                encoding='utf-8',  # um.exe 输出通常是 utf-8
                errors='replace',  # 防止乱码导致崩溃
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            # 实时读取输出
            while self.running:
                line = self.process.stdout.readline()
                if not line and self.process.poll() is not None:
                    break
                if line:
                    clean_line = line.strip()
                    if clean_line:
                        self.log(f"CLI > {clean_line}")

            rc = self.process.poll()
            if rc == 0:
                self.log("✅ 任务执行完毕！所有文件处理成功。")
            else:
                self.log(f"⚠️ 任务结束，返回代码: {rc} (如果不为0可能存在部分错误)")

        except Exception as e:
            self.log(f"❌ 发生系统错误: {str(e)}")
        finally:
            self.running = False
            self.process = None

    def stop(self):
        self.running = False
        if self.process:
            self.process.terminate()
            self.log("🛑 正在尝试停止进程...")


# ================= 前端 UI (CustomTkinter) =================
class MusicApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.logic = BackendLogic(self.log)

        # 窗口设置
        self.title("Unlock Music GUI (Pro)")
        self.geometry("850x600")
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        # 布局 Grid
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- 左侧侧边栏 (设置) ---
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(self.sidebar, text="⚙️ 核心设置", font=("微软雅黑", 20, "bold")).grid(row=0, column=0, padx=20,
                                                                                           pady=20)

        # UM.exe 设置
        ctk.CTkLabel(self.sidebar, text="核心程序 (um.exe):", anchor="w").grid(row=1, column=0, padx=20, pady=(10, 0),
                                                                               sticky="w")
        self.um_entry = ctk.CTkEntry(self.sidebar, placeholder_text="选择 um.exe")
        self.um_entry.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="ew")
        self.um_entry.insert(0, self.config.get("um_path"))

        ctk.CTkButton(self.sidebar, text="浏览文件...", command=self.select_um_exe, fg_color="#444").grid(row=3,
                                                                                                          column=0,
                                                                                                          padx=20,
                                                                                                          pady=10)

        # 底部信息
        ctk.CTkLabel(self.sidebar, text="Based on Unlock Music CLI\nGUI by CustomTkinter", text_color="gray",
                     font=("Arial", 10)).grid(row=5, column=0, padx=20, pady=20)

        # --- 右侧主内容 ---
        self.main_area = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)

        # 标题
        ctk.CTkLabel(self.main_area, text="🎧 音乐解密 & 信息补全", font=("微软雅黑", 22, "bold")).pack(anchor="w",
                                                                                                       pady=(0, 20))

        # 1. 文件夹选择区域
        self.path_frame = ctk.CTkFrame(self.main_area)
        self.path_frame.pack(fill="x", pady=10)

        # 输入
        ctk.CTkLabel(self.path_frame, text="加密源目录 (包含 .mflac/.mgg 等):").pack(anchor="w", padx=15, pady=(15, 5))
        self.input_entry = self._create_path_selector(self.path_frame, "input_dir")

        # 输出
        ctk.CTkLabel(self.path_frame, text="处理输出目录:").pack(anchor="w", padx=15, pady=(10, 5))
        self.output_entry = self._create_path_selector(self.path_frame, "output_dir")

        # 2. 选项区域
        self.opt_frame = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.opt_frame.pack(fill="x", pady=10)

        self.check_meta_var = ctk.BooleanVar(value=self.config.get("auto_meta"))
        self.check_meta = ctk.CTkSwitch(self.opt_frame, text="启用自动补全元数据 (联网获取封面、歌名、专辑)",
                                        variable=self.check_meta_var,
                                        command=lambda: self.config.save_config("auto_meta", self.check_meta_var.get()),
                                        progress_color="#2CC985")
        self.check_meta.pack(side="left")

        # 3. 运行按钮
        self.btn_run = ctk.CTkButton(self.main_area, text="🚀 开始批量处理", height=50, font=("微软雅黑", 16, "bold"),
                                     command=self.start_process)
        self.btn_run.pack(pady=20, fill="x")

        # 4. 日志区
        ctk.CTkLabel(self.main_area, text="控制台输出:", anchor="w").pack(fill="x", pady=(5, 0))
        self.log_box = ctk.CTkTextbox(self.main_area, height=180, font=("Consolas", 12))
        self.log_box.pack(fill="both", expand=True, pady=(5, 0))
        self.log_box.configure(state="disabled")

    def _create_path_selector(self, parent, config_key):
        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.pack(fill="x", padx=15, pady=(0, 15))

        entry = ctk.CTkEntry(container)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.insert(0, self.config.get(config_key))

        btn = ctk.CTkButton(container, text="📂", width=40, command=lambda: self.select_folder(entry, config_key))
        btn.pack(side="right")
        return entry

    # --- 交互逻辑 ---
    def select_folder(self, entry_widget, key):
        path = filedialog.askdirectory()
        if path:
            entry_widget.delete(0, "end")
            entry_widget.insert(0, path)
            self.config.save_config(key, path)

    def select_um_exe(self):
        path = filedialog.askopenfilename(filetypes=[("Executable", "um.exe"), ("All Files", "*.exe")])
        if path:
            self.um_entry.delete(0, "end")
            self.um_entry.insert(0, path)
            self.config.save_config("um_path", path)

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def toggle_ui(self, enable):
        state = "normal" if enable else "disabled"
        self.btn_run.configure(state=state, text="🚀 开始批量处理" if enable else "⏳ 处理中...")

    def start_process(self):
        um_path = self.um_entry.get()
        inp = self.input_entry.get()
        out = self.output_entry.get()
        meta = self.check_meta_var.get()

        if not um_path or not inp or not out:
            messagebox.showerror("参数错误", "请确保 um.exe 路径、输入目录和输出目录都已设置！")
            return

        self.toggle_ui(False)
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")  # 清空日志
        self.log_box.configure(state="disabled")

        # 开启线程运行
        threading.Thread(target=self._run_thread, args=(um_path, inp, out, meta), daemon=True).start()

    def _run_thread(self, um, inp, out, meta):
        self.logic.run_um_process(um, inp, out, meta)
        # 任务结束回调
        self.after(0, lambda: self.toggle_ui(True))
        self.after(0, lambda: messagebox.showinfo("完成", "队列处理结束！"))


if __name__ == "__main__":
    app = MusicApp()
    app.mainloop()
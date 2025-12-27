import os
import sys
import time
import json
import threading
import subprocess
import shutil
import requests
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from concurrent.futures import ThreadPoolExecutor, as_completed

# 引入 Mutagen 音频处理库
try:
    from mutagen.flac import FLAC, Picture
    from mutagen.mp3 import EasyMP3
    from mutagen.id3 import ID3, APIC
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
except ImportError:
    messagebox.showerror("依赖缺失", "请先安装 mutagen 库: pip install mutagen")
    sys.exit(1)


# ================= 配置管理器 =================
class ConfigManager:
    def __init__(self):
        self.config_file = "um_config.json"
        self.default_config = {
            "um_path": "",
            "input_dir": "",
            "output_dir": "",
            "auto_meta": True
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


# ================= QQ音乐元数据处理器 (Python版) =================
class QQMusicTagger:
    def __init__(self, logger_func):
        self.log = logger_func
        self.headers = {
            "Referer": "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.album_cache = {}
        self.cover_cache = {}
        self.metadata_cache = {}

    def process_directory(self, directory):
        files = [f for f in os.listdir(directory) if f.lower().endswith(('.mp3', '.flac', '.m4a', '.ogg'))]
        if not files:
            self.log("⚠️ 目录中未找到音频文件，跳过标签处理。")
            return

        self.log(f"🎵 开始执行 Python 元数据补全，共 {len(files)} 个文件...")

        success_count = 0
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self._process_single, f, directory): f for f in files}

            for future in as_completed(futures):
                filename = futures[future]
                try:
                    res, msg = future.result()
                    if res:
                        success_count += 1
                        self.log(f"✅ [Tag] {filename} -> {msg}")
                    else:
                        self.log(f"⚠️ [Tag] {filename} -> {msg}")
                except Exception as e:
                    self.log(f"❌ [Tag] {filename} 异常: {e}")

        self.log(f"📊 元数据处理完成：成功 {success_count}/{len(files)}")

    def _process_single(self, filename, directory):
        filepath = os.path.join(directory, filename)

        # 1. 提取基础信息
        artist, title = self._get_local_info(filepath)
        query = f"{artist} {title}".strip()
        if not query:
            query = os.path.splitext(filename)[0]

        # 2. 搜API
        meta = self._search_api(query)
        if not meta:
            return False, "未找到歌曲信息"

        # 3. 补轨道号
        meta['track'] = self._get_track_num(meta)

        # 4. 写标签
        if self._write_tags(filepath, meta):
            return True, f"{meta['title']} - {meta['artist']}"
        return False, "写入失败"

    def _get_local_info(self, path):
        # 简易提取：优先读文件名，因为刚解密的文件标签可能为空
        base = os.path.basename(path)
        name, _ = os.path.splitext(base)
        if " - " in name:
            p = name.split(" - ", 1)
            return p[0].strip(), p[1].strip()
        return "", name.strip()

    def _search_api(self, query):
        if query in self.metadata_cache: return self.metadata_cache[query]

        url = f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?format=json&w={query}&n=1"
        try:
            res = requests.get(url, headers=self.headers, timeout=5).json()
            if not res['data']['song']['list']:
                return None
            song = res['data']['song']['list'][0]

            # 获取封面
            mid = song['albummid']
            cover = ""
            if mid not in self.cover_cache:
                for s in ["800", "500", "300"]:
                    u = f"https://y.qq.com/music/photo_new/T002R{s}x{s}M000{mid}.jpg"
                    try:
                        h = requests.head(u, timeout=3)
                        if h.status_code == 200 and int(h.headers.get('content-length', 0)) > 5000:
                            cover = u;
                            self.cover_cache[mid] = u;
                            break
                    except:
                        continue
            else:
                cover = self.cover_cache[mid]

            data = {
                'title': song['songname'],
                'artist': song['singer'][0]['name'],
                'album': song['albumname'],
                'albummid': mid,
                'songmid': song['songmid'],
                'cover': cover
            }
            self.metadata_cache[query] = data
            return data
        except:
            return None

    def _get_track_num(self, meta):
        mid = meta['albummid']
        if mid in self.album_cache:
            tracks = self.album_cache[mid]
        else:
            try:
                u = f"https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg?albummid={mid}&format=json"
                res = requests.get(u, headers=self.headers, timeout=5).json()
                tracks = res['data']['list']
                self.album_cache[mid] = tracks
            except:
                tracks = []

        for i, t in enumerate(tracks, 1):
            if t['songmid'] == meta['songmid']: return i
        return 1

    def _write_tags(self, path, meta):
        try:
            ext = os.path.splitext(path)[1].lower()
            img_data = None
            if meta['cover']:
                try:
                    img_data = requests.get(meta['cover'], timeout=10).content
                except:
                    pass

            if ext == '.flac':
                audio = FLAC(path)
                audio['title'] = meta['title']
                audio['artist'] = meta['artist']
                audio['album'] = meta['album']
                audio['tracknumber'] = str(meta['track'])
                audio['comment'] = "Processed by 𝗣𝗔𝗡"
                if img_data:
                    p = Picture()
                    p.data = img_data
                    p.type = 3
                    p.mime = "image/jpeg"
                    audio.clear_pictures()
                    audio.add_picture(p)
                audio.save()

            elif ext == '.mp3':
                # 先用EasyMP3写文本
                try:
                    audio = EasyMP3(path)
                except:
                    audio = EasyMP3(path)  # 重试或用ID3

                audio['title'] = meta['title']
                audio['artist'] = meta['artist']
                audio['album'] = meta['album']
                audio['tracknumber'] = str(meta['track'])
                audio.save()

                # 再用ID3写封面
                if img_data:
                    audio = ID3(path)
                    audio.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img_data))
                    audio.save()

            elif ext in ['.m4a', '.mp4']:
                audio = MP4(path)
                audio["\xa9nam"] = meta['title']
                audio["\xa9ART"] = meta['artist']
                audio["\xa9alb"] = meta['album']
                audio["trkn"] = [(meta['track'], 0)]
                if img_data:
                    audio["covr"] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()

            return True
        except Exception as e:
            # print(e)
            return False


# ================= 后端逻辑 (整合版) =================
class BackendLogic:
    def __init__(self, logger_func):
        self.log = logger_func
        self.running = False
        self.process = None
        self.tagger = QQMusicTagger(logger_func)

    def run_task(self, um_path, input_dir, output_dir, auto_meta):
        if not os.path.exists(um_path):
            self.log("❌ 错误：未找到 um.exe")
            return
        if not os.path.exists(input_dir):
            self.log("❌ 错误：输入目录不存在")
            return

        self.running = True

        # --- 阶段 1: 使用 um.exe 解密 ---
        try:
            os.makedirs(output_dir, exist_ok=True)
            self.log("🚀 [Phase 1] 启动 um.exe 进行解密...")

            # 不使用 --update-metadata，因为我们后面要自己跑
            cmd = [um_path, '-i', input_dir, '-o', output_dir, '--overwrite']

            # Windows 隐藏窗口设置
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            while self.running:
                line = self.process.stdout.readline()
                if not line and self.process.poll() is not None:
                    break
                if line and line.strip():
                    self.log(f"CLI > {line.strip()}")

            if self.process.poll() != 0:
                self.log("⚠️ 解密过程可能存在错误，请检查日志。")
            else:
                self.log("✅ 解密完成。")

        except Exception as e:
            self.log(f"❌ 解密阶段发生错误: {e}")
            self.running = False
            return

        # --- 阶段 2: 使用 Python 补全元数据 ---
        if self.running and auto_meta:
            self.log("\n🚀 [Phase 2] 启动 Python 脚本补全元数据 (QQ音乐源)...")
            try:
                self.tagger.process_directory(output_dir)
            except Exception as e:
                self.log(f"❌ 元数据处理阶段错误: {e}")

        self.running = False


# ================= 前端 UI (CustomTkinter) =================
class MusicApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.logic = BackendLogic(self.log)

        # 窗口设置
        self.title("Unlock Music GUI (Pro)")
        self.geometry("850x650")
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 侧边栏
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(self.sidebar, text="⚙️ 设置", font=("微软雅黑", 20, "bold")).grid(row=0, column=0, padx=20,
                                                                                       pady=20)

        ctk.CTkLabel(self.sidebar, text="核心程序 (um.exe):", anchor="w").grid(row=1, column=0, padx=20, pady=(10, 0),
                                                                               sticky="w")
        self.um_entry = ctk.CTkEntry(self.sidebar)
        self.um_entry.grid(row=2, column=0, padx=20, pady=(0, 10), sticky="ew")
        self.um_entry.insert(0, self.config.get("um_path"))
        ctk.CTkButton(self.sidebar, text="浏览...", command=self.select_um_exe, fg_color="#444").grid(row=3, column=0,
                                                                                                      padx=20, pady=10)

        ctk.CTkLabel(self.sidebar, text="Powered by Unlock Music\nMetadata by QQMusic Api", text_color="gray",
                     font=("Arial", 10)).grid(row=5, column=0, padx=20, pady=20)

        # 主内容
        self.main_area = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)

        ctk.CTkLabel(self.main_area, text="🎧 音乐解密 & 智能整理", font=("微软雅黑", 22, "bold")).pack(anchor="w",
                                                                                                       pady=(0, 20))

        # 路径选择
        self.path_frame = ctk.CTkFrame(self.main_area)
        self.path_frame.pack(fill="x", pady=10)

        ctk.CTkLabel(self.path_frame, text="加密源目录:").pack(anchor="w", padx=15, pady=(15, 5))
        self.input_entry = self._create_path_selector(self.path_frame, "input_dir")

        ctk.CTkLabel(self.path_frame, text="输出目录:").pack(anchor="w", padx=15, pady=(10, 5))
        self.output_entry = self._create_path_selector(self.path_frame, "output_dir")

        # 选项
        self.opt_frame = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.opt_frame.pack(fill="x", pady=10)

        self.check_meta_var = ctk.BooleanVar(value=self.config.get("auto_meta"))
        self.check_meta = ctk.CTkSwitch(self.opt_frame, text="启用自动补全元数据 (使用 Python 爬虫逻辑)",
                                        variable=self.check_meta_var,
                                        command=lambda: self.config.save_config("auto_meta", self.check_meta_var.get()),
                                        progress_color="#2CC985")
        self.check_meta.pack(side="left")

        # 运行按钮
        self.btn_run = ctk.CTkButton(self.main_area, text="🚀 开始处理", height=50, font=("微软雅黑", 16, "bold"),
                                     command=self.start_process)
        self.btn_run.pack(pady=20, fill="x")

        # 日志
        self.log_box = ctk.CTkTextbox(self.main_area, height=180, font=("Consolas", 11))
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

    def select_folder(self, entry, key):
        path = filedialog.askdirectory()
        if path:
            entry.delete(0, "end");
            entry.insert(0, path)
            self.config.save_config(key, path)

    def select_um_exe(self):
        path = filedialog.askopenfilename(filetypes=[("Executable", "um.exe"), ("All", "*.exe")])
        if path:
            self.um_entry.delete(0, "end");
            self.um_entry.insert(0, path)
            self.config.save_config("um_path", path)

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def toggle_ui(self, enable):
        state = "normal" if enable else "disabled"
        self.btn_run.configure(state=state, text="🚀 开始处理" if enable else "⏳ 运行中...")

    def start_process(self):
        um = self.um_entry.get()
        inp = self.input_entry.get()
        out = self.output_entry.get()
        meta = self.check_meta_var.get()

        if not um or not inp or not out:
            messagebox.showerror("参数错误", "请检查路径设置")
            return

        self.toggle_ui(False)
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        threading.Thread(target=self._run_thread, args=(um, inp, out, meta), daemon=True).start()

    def _run_thread(self, um, inp, out, meta):
        self.logic.run_task(um, inp, out, meta)
        self.after(0, lambda: self.toggle_ui(True))
        self.after(0, lambda: messagebox.showinfo("完成", "处理流程结束！"))


if __name__ == "__main__":
    app = MusicApp()
    app.mainloop()
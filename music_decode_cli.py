import os
import sys
import time
import json
import threading
import subprocess
import shutil
import requests
import re
import difflib
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


# ================= 优化版元数据处理器 (仅补全封面和音轨) =================
class QQMusicTagger:
    def __init__(self, logger_func):
        self.log = logger_func
        self.headers = {
            "Referer": "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.album_cache = {}

    def process_directory(self, directory):
        valid_exts = ('.mp3', '.flac', '.m4a', '.ogg')
        files = [f for f in os.listdir(directory) if f.lower().endswith(valid_exts)]
        if not files:
            self.log("⚠️ 目录中未找到解密后的音乐文件。")
            return

        self.log(f"🎵 正在精准补全封面与音轨号 (#)，共 {len(files)} 个文件...")

        success_count = 0
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self._process_single, f, directory): f for f in files}

            for future in as_completed(futures):
                filename = futures[future]
                try:
                    res, msg = future.result()
                    if res:
                        success_count += 1
                        self.log(f"✅ [补全成功] {filename} -> {msg}")
                    else:
                        self.log(f"⚠️ [跳过] {filename} -> {msg}")
                except Exception as e:
                    self.log(f"❌ [异常] {filename}: {e}")

        self.log(f"📊 补全任务结束：成功 {success_count}/{len(files)}")

    def _process_single(self, filename, directory):
        filepath = os.path.join(directory, filename)

        # 1. 仅读取本地现有的标题和专辑名，用于云端匹配
        local_title, local_artist, local_album = self._read_local_tags(filepath)

        # 即使读不到也尝试从文件名解析，保证搜索能进行
        if not local_title or not local_album:
            raw_artist, raw_title = self._get_info_from_filename(filename)
            local_title = local_title or raw_title
            local_artist = local_artist or raw_artist
            local_album = local_album or "未知专辑"

        # 2. 搜索专辑以获取 albummid
        album_meta = self._search_album(local_album, local_artist)
        if not album_meta:
            # 备选：按歌曲搜获取专辑信息
            album_meta = self._search_song_fallback(local_title, local_artist)
            if not album_meta: return False, "云端匹配失败"

        # 3. 在专辑中查找本首歌的音轨号 (#)
        track_index = self._find_track_index_in_album(album_meta['albummid'], local_title)

        # 4. 构造补全数据：仅包含封面 URL 和音轨号
        patch_data = {
            'track': track_index,
            'cover': f"https://y.qq.com/music/photo_new/T002R800x800M000{album_meta['albummid']}.jpg"
        }

        # 5. 写入（仅写入封面和音轨）
        if self._write_patch_only(filepath, patch_data):
            return True, f"已补全封面 & 音轨 #{track_index}"
        return False, "写入失败"

    def _read_local_tags(self, path):
        try:
            ext = os.path.splitext(path)[1].lower()
            t, r, a = "", "", ""
            if ext == '.flac':
                audio = FLAC(path)
                t = audio.get('title', [""])[0]
                r = audio.get('artist', [""])[0]
                a = audio.get('album', [""])[0]
            elif ext == '.mp3':
                audio = EasyMP3(path)
                t = audio.get('title', [""])[0]
                r = audio.get('artist', [""])[0]
                a = audio.get('album', [""])[0]
            elif ext in ['.m4a', '.mp4']:
                audio = MP4(path)
                t = audio.get('\xa9nam', [""])[0]
                r = audio.get('\xa9ART', [""])[0]
                a = audio.get('\xa9alb', [""])[0]
            return t, r, a
        except:
            return "", "", ""

    def _get_info_from_filename(self, filename):
        name = os.path.splitext(filename)[0]
        name = re.sub(r'(_EM|_HQ|_SQ|_24bit)', '', name, flags=re.I)
        if " - " in name:
            parts = name.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return "", name.strip()

    def _search_album(self, album_name, artist_name):
        if not album_name or album_name == "未知专辑": return None
        query = f"{artist_name} {album_name}" if artist_name else album_name
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {"format": "json", "w": query, "n": 5, "t": 8}  # t=8 搜专辑
        try:
            res = requests.get(url, params=params, headers=self.headers, timeout=5).json()
            albums = res.get('data', {}).get('album', {}).get('list', [])
            if albums:
                return {'albummid': albums[0]['albumMID'], 'albumname': albums[0]['albumName']}
        except:
            pass
        return None

    def _search_song_fallback(self, title, artist):
        query = f"{artist} {title}"
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {"format": "json", "w": query, "n": 5}
        try:
            res = requests.get(url, params=params, headers=self.headers, timeout=5).json()
            songs = res.get('data', {}).get('song', {}).get('list', [])
            if songs:
                return {'albummid': songs[0]['albummid'], 'albumname': songs[0]['albumname']}
        except:
            pass
        return None

    def _find_track_index_in_album(self, albummid, song_title):
        if albummid in self.album_cache:
            tracks = self.album_cache[albummid]
        else:
            try:
                url = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg"
                res = requests.get(url, params={"albummid": albummid, "format": "json"}, headers=self.headers,
                                   timeout=5).json()
                tracks = res.get('data', {}).get('list', [])
                self.album_cache[albummid] = tracks
            except:
                tracks = []

        for i, t in enumerate(tracks, 1):
            if difflib.SequenceMatcher(None, song_title.lower(), t['songname'].lower()).ratio() > 0.8:
                return i
        return 1

    def _write_patch_only(self, path, patch):
        """核心修改：只写入音轨号和封面，不改动标题、歌手、专辑名"""
        try:
            ext = os.path.splitext(path)[1].lower()
            img_data = None
            try:
                img_data = requests.get(patch['cover'], timeout=10).content
            except:
                pass

            if ext == '.flac':
                audio = FLAC(path)
                # 仅写音轨
                audio['tracknumber'] = str(patch['track'])
                # 仅写封面
                if img_data:
                    p = Picture();
                    p.data = img_data;
                    p.type = 3;
                    p.mime = "image/jpeg"
                    audio.clear_pictures();
                    audio.add_picture(p)
                audio.save()
            elif ext == '.mp3':
                # 仅写音轨
                audio = EasyMP3(path)
                audio['tracknumber'] = str(patch['track'])
                audio.save()
                # 仅写封面
                if img_data:
                    audio = ID3(path)
                    audio.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img_data))
                    audio.save()
            elif ext in ['.m4a', '.mp4']:
                audio = MP4(path)
                # 仅写音轨 (trkn 格式为 [(index, total)])
                audio["trkn"] = [(patch['track'], 0)]
                # 仅写封面
                if img_data:
                    audio["covr"] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()
            return True
        except:
            return False


# ================= 后端逻辑 =================
class BackendLogic:
    def __init__(self, logger_func):
        self.log = logger_func
        self.running = False
        self.tagger = QQMusicTagger(logger_func)

    def run_task(self, um_path, input_dir, output_dir, auto_meta):
        if not os.path.exists(um_path):
            self.log("❌ 找不到 um.exe")
            return
        self.running = True
        try:
            os.makedirs(output_dir, exist_ok=True)
            self.log("🚀 [Phase 1] 正在解密文件...")
            audio_exts = ('.mflac', '.mgg', '.mmp4', '.qmc', '.kgm', '.vpr', '.ncm')
            all_files = [f for f in os.listdir(input_dir) if f.lower().endswith(audio_exts)]

            for f in all_files:
                if not self.running: break
                f_path = os.path.join(input_dir, f)
                cmd = [um_path, '-i', f_path, '-o', output_dir, '--overwrite']
                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.run(cmd, startupinfo=startupinfo,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, check=False)

            self.log("✅ 解密完成。")
            if auto_meta and self.running:
                self.log("\n🚀 [Phase 2] 正在检索云端信息并补全封面与音轨号 (#)...")
                self.tagger.process_directory(output_dir)
        except Exception as e:
            self.log(f"❌ 运行异常: {e}")
        finally:
            self.running = False


# ================= UI 布局 =================
class MusicApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.logic = BackendLogic(self.log)
        self.title("Unlock Music GUI (封面 & 音轨补全版)")
        self.geometry("900x700")
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        ctk.CTkLabel(self.sidebar, text="⚙️ 设置", font=("微软雅黑", 20, "bold")).pack(pady=20)
        self.um_entry = ctk.CTkEntry(self.sidebar);
        self.um_entry.pack(padx=20, pady=5, fill="x")
        self.um_entry.insert(0, self.config.get("um_path"))
        ctk.CTkButton(self.sidebar, text="选择 um.exe", command=self.select_um_exe).pack(pady=10)

        self.main_area = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=25, pady=25)
        ctk.CTkLabel(self.main_area, text="🎼 封面与音轨批量补全工具", font=("微软雅黑", 24, "bold")).pack(anchor="w",
                                                                                                          pady=(0, 20))

        self.path_frame = ctk.CTkFrame(self.main_area)
        self.path_frame.pack(fill="x", pady=10)
        self.input_entry = self._create_path_selector(self.path_frame, "input_dir", "加密文件目录:")
        self.output_entry = self._create_path_selector(self.path_frame, "output_dir", "解密输出目录:")

        self.check_meta_var = ctk.BooleanVar(value=self.config.get("auto_meta"))
        ctk.CTkSwitch(self.main_area, text="仅补全封面与音轨 (保留原始标题/歌手/专辑名)", variable=self.check_meta_var,
                      command=lambda: self.config.save_config("auto_meta", self.check_meta_var.get())).pack(pady=15)

        self.btn_run = ctk.CTkButton(self.main_area, text="🚀 开始任务", height=50, font=("微软雅黑", 18, "bold"),
                                     command=self.start_process)
        self.btn_run.pack(pady=10, fill="x")

        self.log_box = ctk.CTkTextbox(self.main_area, height=350, font=("Consolas", 11))
        self.log_box.pack(fill="both", expand=True, pady=10)
        self.log_box.configure(state="disabled")

    def _create_path_selector(self, parent, config_key, label):
        ctk.CTkLabel(parent, text=label).pack(anchor="w", padx=15, pady=(10, 0))
        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.pack(fill="x", padx=15, pady=(0, 10))
        entry = ctk.CTkEntry(container);
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.insert(0, self.config.get(config_key))
        ctk.CTkButton(container, text="📂", width=45, command=lambda: self.select_folder(entry, config_key)).pack(
            side="right")
        return entry

    def select_folder(self, entry, key):
        path = filedialog.askdirectory()
        if path: entry.delete(0, "end"); entry.insert(0, path); self.config.save_config(key, path)

    def select_um_exe(self):
        path = filedialog.askopenfilename()
        if path: self.um_entry.delete(0, "end"); self.um_entry.insert(0, path); self.config.save_config("um_path", path)

    def log(self, msg):
        self.log_box.configure(state="normal");
        self.log_box.insert("end", f"{msg}\n");
        self.log_box.see("end");
        self.log_box.configure(state="disabled")

    def start_process(self):
        self.log_box.configure(state="normal");
        self.log_box.delete("1.0", "end");
        self.log_box.configure(state="disabled")
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self):
        self.btn_run.configure(state="disabled", text="⚡ 正在补全中...")
        self.logic.run_task(self.um_entry.get(), self.input_entry.get(), self.output_entry.get(),
                            self.check_meta_var.get())
        self.btn_run.configure(state="normal", text="🚀 开始任务")
        messagebox.showinfo("完成", "任务结束，已成功补全缺失的封面与音轨号。")


if __name__ == "__main__":
    app = MusicApp()
    app.mainloop()
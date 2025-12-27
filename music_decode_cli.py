import os
import sys
import time
import json
import threading
import subprocess
import requests
import re
import difflib
import shutil
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
            "patch_input_dir": "",
            "patch_output_dir": "",
            "auto_meta": True,
            "del_src_dec": False,
            "del_src_patch": False
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
        return self.config.get(key, self.default_config.get(key, ""))


# ================= 元数据处理器 =================
class QQMusicTagger:
    def __init__(self, logger_func):
        self.log = logger_func
        self.headers = {
            "Referer": "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.album_cache = {}

    def process_directory(self, input_dir, output_dir, delete_original=False):
        valid_exts = ('.mp3', '.flac', '.m4a', '.ogg')
        files = [f for f in os.listdir(input_dir) if f.lower().endswith(valid_exts)]
        if not files:
            self.log("⚠️ 目录中未找到可处理的音频文件。")
            return

        self.log(f"🎵 正在补全元数据，共 {len(files)} 个文件...")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        success_count = 0
        is_same_dir = (os.path.abspath(input_dir) == os.path.abspath(output_dir))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self._process_single, f, input_dir, output_dir, is_same_dir, delete_original): f
                       for f in files}
            for future in as_completed(futures):
                filename = futures[future]
                try:
                    res, msg = future.result()
                    if res:
                        success_count += 1
                        self.log(f"✅ [成功] {filename} -> {msg}")
                    else:
                        self.log(f"⚠️ [跳过] {filename} -> {msg}")
                except Exception as e:
                    self.log(f"❌ [异常] {filename}: {e}")

        self.log(f"📊 任务结束：成功 {success_count}/{len(files)}")

    def _process_single(self, filename, input_dir, output_dir, is_same_dir, delete_original):
        src_path = os.path.join(input_dir, filename)
        dst_path = os.path.join(output_dir, filename)

        work_path = src_path
        if not is_same_dir:
            shutil.copy2(src_path, dst_path)
            work_path = dst_path

        local_title, local_artist, local_album = self._read_local_tags(work_path)
        if not local_title or not local_album:
            raw_artist, raw_title = self._get_info_from_filename(filename)
            local_title = local_title or raw_title
            local_artist = local_artist or raw_artist
            local_album = local_album or "未知专辑"

        album_meta = self._search_album(local_album, local_artist)
        if not album_meta:
            album_meta = self._search_song_fallback(local_title, local_artist)
            if not album_meta: return False, "无法匹配云端信息"

        track_index = self._find_track_index_in_album(album_meta['albummid'], local_title)
        patch_data = {
            'track': track_index,
            'cover': f"https://y.qq.com/music/photo_new/T002R800x800M000{album_meta['albummid']}.jpg"
        }

        if self._write_patch_only(work_path, patch_data):
            # 只有在不同文件夹且勾选了删除时才执行
            if not is_same_dir and delete_original:
                try:
                    os.remove(src_path)
                except:
                    pass
            return True, f"已更新封面 & 音轨 #{track_index}"

        return False, "元数据写入失败"

    def _read_local_tags(self, path):
        try:
            ext = os.path.splitext(path)[1].lower()
            t, r, a = "", "", ""
            if ext == '.flac':
                audio = FLAC(path)
                t = audio.get('title', [""])[0];
                r = audio.get('artist', [""])[0];
                a = audio.get('album', [""])[0]
            elif ext == '.mp3':
                audio = EasyMP3(path)
                t = audio.get('title', [""])[0];
                r = audio.get('artist', [""])[0];
                a = audio.get('album', [""])[0]
            elif ext in ['.m4a', '.mp4']:
                audio = MP4(path)
                t = audio.get('\xa9nam', [""])[0];
                r = audio.get('\xa9ART', [""])[0];
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
        params = {"format": "json", "w": query, "n": 5, "t": 8}
        try:
            res = requests.get(url, params=params, headers=self.headers, timeout=5).json()
            albums = res.get('data', {}).get('album', {}).get('list', [])
            if albums: return {'albummid': albums[0]['albumMID'], 'albumname': albums[0]['albumName']}
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
            if songs: return {'albummid': songs[0]['albummid'], 'albumname': songs[0]['albumname']}
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
        try:
            ext = os.path.splitext(path)[1].lower()
            img_data = None
            try:
                img_data = requests.get(patch['cover'], timeout=10).content
            except:
                pass

            if ext == '.flac':
                audio = FLAC(path)
                audio['tracknumber'] = str(patch['track'])
                if img_data:
                    p = Picture();
                    p.data = img_data;
                    p.type = 3;
                    p.mime = "image/jpeg"
                    audio.clear_pictures();
                    audio.add_picture(p)
                audio.save()
            elif ext == '.mp3':
                audio = EasyMP3(path)
                audio['tracknumber'] = str(patch['track']);
                audio.save()
                if img_data:
                    audio = ID3(path)
                    audio.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img_data))
                    audio.save()
            elif ext in ['.m4a', '.mp4']:
                audio = MP4(path)
                audio["trkn"] = [(patch['track'], 0)]
                if img_data: audio["covr"] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
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

    def run_decrypt(self, um_path, input_dir, output_dir, auto_patch=False, delete_src=False):
        if not os.path.exists(um_path):
            self.log("❌ 找不到引擎文件")
            return
        self.running = True
        try:
            os.makedirs(output_dir, exist_ok=True)
            self.log("🚀 [解密] 启动批量任务...")
            audio_exts = ('.mflac', '.mgg', '.mmp4', '.qmc', '.kgm', '.vpr', '.ncm')
            all_files = [f for f in os.listdir(input_dir) if f.lower().endswith(audio_exts)]

            is_same_dir = (os.path.abspath(input_dir) == os.path.abspath(output_dir))

            for i, f in enumerate(all_files, 1):
                if not self.running: break
                f_path = os.path.join(input_dir, f)
                cmd = [um_path, '-i', f_path, '-o', output_dir, '--overwrite']

                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                subprocess.run(cmd, startupinfo=startupinfo,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, check=False)

                # 解密页规则：同文件夹强制删加密文件，不同文件夹看开关
                if is_same_dir or delete_src:
                    try:
                        os.remove(f_path)
                    except:
                        pass

                if i % 5 == 0 or i == len(all_files):
                    self.log(f"   ↳ 进度: {i}/{len(all_files)}")

            self.log("✅ 解密已完成。")
            if auto_patch and self.running:
                self.log("\n🚀 [自动补全] 正在请求云端数据...")
                self.tagger.process_directory(output_dir, output_dir, False)  # 自动模式下不二次删除
        except Exception as e:
            self.log(f"❌ 错误: {e}")
        finally:
            self.running = False

    def run_patch_only(self, input_dir, output_dir, delete_src):
        self.running = True
        try:
            self.tagger.process_directory(input_dir, output_dir, delete_src)
        except Exception as e:
            self.log(f"❌ 补全异常: {e}")
        finally:
            self.running = False


# ================= UI 布局 =================
class MusicApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.logic = BackendLogic(self.log)

        self.title("Unlock Music Tool Pro")
        self.geometry("1000x800")
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 1. 侧边栏
        self.sidebar = ctk.CTkFrame(self, width=240, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(self.sidebar, text="🎼 UMT Pro", font=("微软雅黑", 24, "bold")).pack(pady=(30, 20))
        ctk.CTkLabel(self.sidebar, text="选择引擎路径", font=("微软雅黑", 13, "bold")).pack(anchor="w", padx=20)

        um_row = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        um_row.pack(fill="x", padx=15, pady=5)
        self.um_entry = ctk.CTkEntry(um_row, height=35, placeholder_text="um.exe 路径")
        self.um_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.um_entry.insert(0, self.config.get("um_path"))
        ctk.CTkButton(um_row, text="浏览", width=50, height=35, command=self.select_um_exe).pack(side="right")

        self.info_label = ctk.CTkLabel(self.sidebar, text="Version 2.8\nby PAN", font=("Consolas", 11),
                                       text_color="gray")
        self.info_label.pack(side="bottom", pady=25)

        # 2. 主体区域
        self.main_container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_container.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_container.grid_rowconfigure(1, weight=1)
        self.main_container.grid_columnconfigure(0, weight=1)

        self.nav_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.nav_frame.grid(row=0, column=0, sticky="ew", pady=(0, 15))

        self.btn_tab_decrypt = ctk.CTkButton(self.nav_frame, text="批量解密", width=120, height=40, corner_radius=8,
                                             command=lambda: self.switch_tab("decrypt"))
        self.btn_tab_decrypt.pack(side="left", padx=(0, 10))
        self.btn_tab_patch = ctk.CTkButton(self.nav_frame, text="元数据补全", width=120, height=40, corner_radius=8,
                                           fg_color="gray", command=lambda: self.switch_tab("patch"))
        self.btn_tab_patch.pack(side="left")

        self.content_frame = ctk.CTkFrame(self.main_container, corner_radius=15)
        self.content_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        self.tab_decrypt = self._create_decrypt_page()
        self.tab_patch = self._create_patch_page()
        self.switch_tab("decrypt")

        # 3. 日志
        self.log_box = ctk.CTkTextbox(self.main_container, height=220, font=("Consolas", 12), corner_radius=10)
        self.log_box.grid(row=2, column=0, sticky="ew", pady=(15, 0))
        self.log_box.configure(state="disabled")

    def _create_decrypt_page(self):
        page = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        ctk.CTkLabel(page, text="批量音乐解密", font=("微软雅黑", 20, "bold")).pack(anchor="w", padx=25, pady=(20, 10))

        self.dec_input = self._create_path_row(page, "input_dir", "加密资源文件夹:")
        self.dec_output = self._create_path_row(page, "output_dir", "解密保存文件夹:")

        self.auto_patch_var = ctk.BooleanVar(value=self.config.get("auto_meta"))
        ctk.CTkSwitch(page, text="解密后自动执行元数据补全", variable=self.auto_patch_var,
                      command=lambda: self.config.save_config("auto_meta", self.auto_patch_var.get())).pack(anchor="w",
                                                                                                            padx=25,
                                                                                                            pady=(
                                                                                                            15, 5))

        self.del_src_dec_var = ctk.BooleanVar(value=self.config.get("del_src_dec"))
        ctk.CTkSwitch(page, text="完成后删除原始加密文件 (同文件夹时强制执行)", variable=self.del_src_dec_var,
                      command=lambda: self.config.save_config("del_src_dec", self.del_src_dec_var.get())).pack(
            anchor="w", padx=25, pady=5)

        self.btn_run_dec = ctk.CTkButton(page, text="🔥 开始执行解密任务", height=50, font=("微软雅黑", 16, "bold"),
                                         command=self.start_decrypt)
        self.btn_run_dec.pack(padx=25, pady=(25, 10), fill="x")
        return page

    def _create_patch_page(self):
        page = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        ctk.CTkLabel(page, text="元数据补全", font=("微软雅黑", 20, "bold")).pack(anchor="w", padx=25, pady=(20, 10))

        self.patch_input = self._create_path_row(page, "patch_input_dir", "待补全文件夹 (初始):")
        self.patch_output = self._create_path_row(page, "patch_output_dir", "保存文件夹 (最终):")

        self.del_src_patch_var = ctk.BooleanVar(value=self.config.get("del_src_patch"))
        ctk.CTkSwitch(page, text="移动至最终文件夹后删除原文件 (同文件夹时不生效)", variable=self.del_src_patch_var,
                      command=lambda: self.config.save_config("del_src_patch", self.del_src_patch_var.get())).pack(
            anchor="w", padx=25, pady=15)

        self.btn_run_patch = ctk.CTkButton(page, text="✨ 开始补全云端信息", height=50, font=("微软雅黑", 16, "bold"),
                                           fg_color="#2b719e", command=self.start_patch_only)
        self.btn_run_patch.pack(padx=25, pady=25, fill="x")
        return page

    def _create_path_row(self, parent, config_key, label):
        ctk.CTkLabel(parent, text=label).pack(anchor="w", padx=25, pady=(12, 0))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=25, pady=5)
        entry = ctk.CTkEntry(row, height=35)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.insert(0, self.config.get(config_key))
        ctk.CTkButton(row, text="浏览", width=70, height=35,
                      command=lambda: self.select_folder(entry, config_key)).pack(side="right")
        return entry

    def switch_tab(self, name):
        if name == "decrypt":
            self.tab_patch.pack_forget()
            self.tab_decrypt.pack(fill="both", expand=True)
            self.btn_tab_decrypt.configure(fg_color=["#3B8ED0", "#1F6AA5"])
            self.btn_tab_patch.configure(fg_color="gray")
        else:
            self.tab_decrypt.pack_forget()
            self.tab_patch.pack(fill="both", expand=True)
            self.btn_tab_patch.configure(fg_color=["#3B8ED0", "#1F6AA5"])
            self.btn_tab_decrypt.configure(fg_color="gray")

    def select_folder(self, entry, key):
        path = filedialog.askdirectory()
        if path:
            entry.delete(0, "end");
            entry.insert(0, path)
            self.config.save_config(key, path)

    def select_um_exe(self):
        path = filedialog.askopenfilename(filetypes=[("Executable", "*.exe")])
        if path:
            self.um_entry.delete(0, "end");
            self.um_entry.insert(0, path)
            self.config.save_config("um_path", path)

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start_decrypt(self):
        self.log_box.configure(state="normal");
        self.log_box.delete("1.0", "end");
        self.log_box.configure(state="disabled")
        threading.Thread(target=self._exec_dec, daemon=True).start()

    def _exec_dec(self):
        self.btn_run_dec.configure(state="disabled", text="⚡ 正在处理...")
        self.logic.run_decrypt(self.um_entry.get(), self.dec_input.get(), self.dec_output.get(),
                               self.auto_patch_var.get(), self.del_src_dec_var.get())
        self.btn_run_dec.configure(state="normal", text="🔥 开始执行解密任务")
        messagebox.showinfo("完成", "任务流已结束")

    def start_patch_only(self):
        self.log_box.configure(state="normal");
        self.log_box.delete("1.0", "end");
        self.log_box.configure(state="disabled")
        threading.Thread(target=self._exec_patch, daemon=True).start()

    def _exec_patch(self):
        self.btn_run_patch.configure(state="disabled", text="⚡ 联机补全中...")
        self.logic.run_patch_only(self.patch_input.get(), self.patch_output.get(), self.del_src_patch_var.get())
        self.btn_run_patch.configure(state="normal", text="✨ 开始补全云端信息")
        messagebox.showinfo("完成", "云端信息补全完毕")


if __name__ == "__main__":
    app = MusicApp()
    app.mainloop()
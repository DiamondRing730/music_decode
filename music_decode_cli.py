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

# Import Mutagen for audio metadata processing
try:
    from mutagen.flac import FLAC, Picture
    from mutagen.mp3 import EasyMP3
    from mutagen.id3 import ID3, APIC
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
except ImportError:
    messagebox.showerror("Missing Dependencies", "Please install mutagen: pip install mutagen")
    sys.exit(1)


# ================= Config Manager =================
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


# ================= Metadata Tagger =================
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

        self.log(f"🎵 正在执行初步处理与多阶段匹配，共 {len(files)} 个文件...")
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
                        self.log(f"✅ [完成] {filename} -> {msg}")
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

        # 1. 提取文件名信息
        f_artist, f_title = self._get_info_from_filename(filename)

        # 2. 初步处理：读取原标签，若歌名不符则对齐，保留原专辑名
        local_title, local_artist, local_album = self._read_local_tags(work_path)

        # 强制将标签歌名向文件名对齐 (只改歌曲名)
        if local_title.lower().strip() != f_title.lower().strip():
            self._write_single_tag(work_path, 'title', f_title)
            local_title = f_title

        # 3. 第一阶段：尝试使用“原专辑名”搜索
        album_meta = None
        if local_album.strip():
            album_meta = self._search_with_album_filter(local_title, f_artist, local_album)

        # 4. 第二阶段：如果第一阶段搜不到，使用“歌名 歌手”回退搜索并更新专辑名
        if not album_meta:
            album_meta = self._search_global(local_title, f_artist)
            if album_meta:
                self._write_single_tag(work_path, 'album', album_meta['albumname'])

        if not album_meta:
            return True, "已同步标题，但未搜到匹配专辑信息"

        # 5. 获取音轨及写入完整元数据 (不再进行最后检查)
        track_index = self._find_track_index_in_album(album_meta['albummid'], local_title)

        patch_data = {
            'title': local_title,
            'artist': f_artist,
            'album': album_meta['albumname'],
            'track': track_index,
            'cover_url': f"https://y.qq.com/music/photo_new/T002R800x800M000{album_meta['albummid']}.jpg"
        }

        if self._write_final_patch(work_path, patch_data):
            if not is_same_dir and delete_original:
                try:
                    os.remove(src_path)
                except:
                    pass
            return True, f"匹配专辑[{patch_data['album']}] (音轨:{track_index})"

        return False, "写入失败"

    def _get_info_from_filename(self, filename):
        name = os.path.splitext(filename)[0]
        name = re.sub(r'(_EM|_HQ|_SQ|_24bit| - 副本)', '', name, flags=re.I).strip()
        if " - " in name:
            parts = name.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return "未知歌手", name

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
            elif ext == '.ogg':
                audio = OggVorbis(path)
                t = audio.get('title', [""])[0];
                r = audio.get('artist', [""])[0];
                a = audio.get('album', [""])[0]
            return str(t), str(r), str(a)
        except:
            return "", "", ""

    def _write_single_tag(self, path, key, value):
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == '.flac':
                audio = FLAC(path);
                audio[key] = value;
                audio.save()
            elif ext == '.mp3':
                audio = EasyMP3(path);
                audio[key] = value;
                audio.save()
            elif ext in ['.m4a', '.mp4']:
                audio = MP4(path)
                k = "\xa9nam" if key == 'title' else "\xa9ART" if key == 'artist' else "\xa9alb"
                audio[k] = [value];
                audio.save()
            elif ext == '.ogg':
                audio = OggVorbis(path);
                audio[key] = value;
                audio.save()
        except:
            pass

    def _search_with_album_filter(self, title, artist, album):
        """第一阶段：带专辑名过滤的精确搜索"""
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {"format": "json", "w": f"{artist} {title} {album}", "n": 5}
        try:
            res = requests.get(url, params=params, headers=self.headers, timeout=5).json()
            songs = res.get('data', {}).get('song', {}).get('list', [])
            for s in songs:
                s_title = s['songname'].lower().strip()
                s_album = s['albumname'].lower().strip()
                # 歌名完全一致，且专辑名高度相似
                if s_title == title.lower().strip():
                    ratio = difflib.SequenceMatcher(None, s_album, album.lower().strip()).ratio()
                    if ratio > 0.8:
                        return {'albummid': s['albummid'], 'albumname': s['albumname']}
            return None
        except:
            return None

    def _search_global(self, title, artist):
        """第二阶段：全局回退搜索 (歌名+歌手)"""
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {"format": "json", "w": f"{artist} {title}", "n": 10}
        try:
            res = requests.get(url, params=params, headers=self.headers, timeout=5).json()
            songs = res.get('data', {}).get('song', {}).get('list', [])
            for s in songs:
                # 只要歌名一致，就采用第一个搜到的专辑
                if s['songname'].lower().strip() == title.lower().strip():
                    return {'albummid': s['albummid'], 'albumname': s['albumname']}
            return None
        except:
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
            if t['songname'].lower().strip() == song_title.lower().strip():
                return i
        return 1  # 找不到则默认第1首

    def _write_final_patch(self, path, patch):
        try:
            ext = os.path.splitext(path)[1].lower()
            img_data = None
            try:
                img_data = requests.get(patch['cover_url'], timeout=10).content
            except:
                pass

            if ext == '.flac':
                audio = FLAC(path)
                audio['title'] = patch['title'];
                audio['artist'] = patch['artist']
                audio['album'] = patch['album'];
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
                audio['title'] = patch['title'];
                audio['artist'] = patch['artist']
                audio['album'] = patch['album'];
                audio['tracknumber'] = str(patch['track'])
                audio.save()
                if img_data:
                    tags = ID3(path);
                    tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img_data));
                    tags.save()
            elif ext in ['.m4a', '.mp4']:
                audio = MP4(path)
                audio["\xa9nam"] = [patch['title']];
                audio["\xa9ART"] = [patch['artist']]
                audio["\xa9alb"] = [patch['album']];
                audio["trkn"] = [(patch['track'], 0)]
                if img_data:
                    audio["covr"] = [MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()
            elif ext == '.ogg':
                audio = OggVorbis(path)
                audio['title'] = patch['title'];
                audio['artist'] = patch['artist']
                audio['album'] = patch['album'];
                audio['tracknumber'] = str(patch['track'])
                if img_data:
                    from base64 import b64encode
                    p = Picture();
                    p.data = img_data;
                    p.type = 3;
                    p.mime = "image/jpeg"
                    audio["metadata_block_picture"] = [b64encode(p.write()).decode('ascii')]
                audio.save()
            return True
        except:
            return False


# ================= Logic Wrapper =================
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

                if is_same_dir or delete_src:
                    try:
                        os.remove(f_path)
                    except:
                        pass

                if i % 5 == 0 or i == len(all_files):
                    self.log(f"   ↳ 进度: {i}/{len(all_files)}")

            self.log("✅ 解密已完成。")
            if auto_patch and self.running:
                self.log("\n🚀 [自动补全] 正在根据文件名对齐并匹配元数据...")
                self.tagger.process_directory(output_dir, output_dir, False)
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


# ================= UI Layout =================
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

        # Sidebar
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

        self.info_label = ctk.CTkLabel(self.sidebar, text="Version 4.6\nRefined Meta Matching", font=("Consolas", 11),
                                       text_color="gray")
        self.info_label.pack(side="bottom", pady=25)

        # Main Area
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

        # Logbox
        self.log_box = ctk.CTkTextbox(self.main_container, height=220, font=("Consolas", 12), corner_radius=10)
        self.log_box.grid(row=2, column=0, sticky="ew", pady=(15, 0))
        self.log_box.configure(state="disabled")

    def _create_decrypt_page(self):
        page = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        ctk.CTkLabel(page, text="批量音乐解密", font=("微软雅黑", 20, "bold")).pack(anchor="w", padx=25, pady=(20, 10))

        self.dec_input = self._create_path_row(page, "input_dir", "加密资源文件夹:")
        self.dec_output = self._create_path_row(page, "output_dir", "解密保存文件夹:")

        self.auto_patch_var = ctk.BooleanVar(value=self.config.get("auto_meta"))
        ctk.CTkSwitch(page, text="解密后启动“文件名对齐”补全", variable=self.auto_patch_var,
                      command=lambda: self.config.save_config("auto_meta", self.auto_patch_var.get())).pack(anchor="w",
                                                                                                            padx=25,
                                                                                                            pady=(
                                                                                                            15, 5))

        self.del_src_dec_var = ctk.BooleanVar(value=self.config.get("del_src_dec"))
        ctk.CTkSwitch(page, text="解密后删除加密文件", variable=self.del_src_dec_var,
                      command=lambda: self.config.save_config("del_src_dec", self.del_src_dec_var.get())).pack(
            anchor="w", padx=25, pady=5)

        self.btn_run_dec = ctk.CTkButton(page, text="🔥 开始执行解密任务", height=50, font=("微软雅黑", 16, "bold"),
                                         command=self.start_decrypt)
        self.btn_run_dec.pack(padx=25, pady=(25, 10), fill="x")
        return page

    def _create_patch_page(self):
        page = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        ctk.CTkLabel(page, text="元数据精确匹配 (初步对齐 + 阶梯搜索)", font=("微软雅黑", 20, "bold")).pack(anchor="w",
                                                                                                            padx=25,
                                                                                                            pady=(
                                                                                                            20, 10))

        self.patch_input = self._create_path_row(page, "patch_input_dir", "音频文件夹 (初始):")
        self.patch_output = self._create_path_row(page, "patch_output_dir", "保存文件夹 (最终):")

        self.del_src_patch_var = ctk.BooleanVar(value=self.config.get("del_src_patch"))
        ctk.CTkSwitch(page, text="处理后删除原文件", variable=self.del_src_patch_var,
                      command=lambda: self.config.save_config("del_src_patch", self.del_src_patch_var.get())).pack(
            anchor="w", padx=25, pady=15)

        self.btn_run_patch = ctk.CTkButton(page, text="✨ 执行初步对齐与搜索补全", height=50,
                                           font=("微软雅黑", 16, "bold"), fg_color="#2b719e",
                                           command=self.start_patch_only)
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
            self.tab_patch.pack_forget();
            self.tab_decrypt.pack(fill="both", expand=True)
            self.btn_tab_decrypt.configure(fg_color=["#3B8ED0", "#1F6AA5"]);
            self.btn_tab_patch.configure(fg_color="gray")
        else:
            self.tab_decrypt.pack_forget();
            self.tab_patch.pack(fill="both", expand=True)
            self.btn_tab_patch.configure(fg_color=["#3B8ED0", "#1F6AA5"]);
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
        self.btn_run_dec.configure(state="disabled", text="Processing...")
        self.logic.run_decrypt(self.um_entry.get(), self.dec_input.get(), self.dec_output.get(),
                               self.auto_patch_var.get(), self.del_src_dec_var.get())
        self.btn_run_dec.configure(state="normal", text="🔥 开始执行解密任务")
        messagebox.showinfo("Success", "Task complete. Variants distinguished by exact matching.")

    def start_patch_only(self):
        self.log_box.configure(state="normal");
        self.log_box.delete("1.0", "end");
        self.log_box.configure(state="disabled")
        threading.Thread(target=self._exec_patch, daemon=True).start()

    def _exec_patch(self):
        self.btn_run_patch.configure(state="disabled", text="Matching...")
        self.logic.run_patch_only(self.patch_input.get(), self.patch_output.get(), self.del_src_patch_var.get())
        self.btn_run_patch.configure(state="normal", text="✨ 执行初步对齐与搜索补全")
        messagebox.showinfo("Success", "Fixed: Titles locked via exact matching logic.")


if __name__ == "__main__":
    app = MusicApp()
    app.mainloop()
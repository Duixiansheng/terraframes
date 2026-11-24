"""TerraFrames Windows desktop application."""
from pathlib import Path
from queue import Empty, Queue
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk
from scene_core import (compare, describe, estimate_registration, export_csv, find_tiffs,
                        inspect, preview, read_patches, sample_windows)


INK = "#172b30"
MUTED = "#4b6568"
ACCENT = "#006956"
PAPER = "#edf3ef"
WHITE = "#ffffff"
PAGE_SIZE = 6


def scan(folder, chosen_reference, recursive, registration, cache, stop, events):
    try:
        paths = find_tiffs(folder, recursive)
        if chosen_reference and chosen_reference not in paths:
            paths.insert(0, chosen_reference)
        if not paths:
            raise ValueError("文件夹中没有找到 .tif 或 .tiff 文件。")
        scenes = []
        for index, path in enumerate(paths, 1):
            if stop.is_set():
                return
            scenes.append(inspect(path))
            events.put(("progress", f"读取文件头  {index}/{len(paths)}"))
        scenes.sort(key=lambda scene: (not scene.date, scene.date, str(scene.path).casefold()))
        available = [scene for scene in scenes if not scene.error]
        if not available:
            raise ValueError("没有成功读取的影像：" + scenes[0].error)
        reference = (next(scene for scene in scenes if scene.path == chosen_reference)
                     if chosen_reference else
                     next((scene for scene in available if scene.georef == "仿射地理定位"), available[0]))
        if reference.error:
            raise ValueError("参考影像不可读取：" + reference.error)
        events.put(("reference", reference))
        windows = sample_windows(reference) if registration and reference.georef == "仿射地理定位" else []
        try:
            patches = list(read_patches(reference, reference, windows)) if windows else []
        except Exception as exc:
            windows = []
            reference.findings.append(f"参考图无法读取配准样本：{exc}")
        for index, scene in enumerate(scenes, 1):
            if stop.is_set():
                return
            compare(scene, reference)
            thumbnail = None
            if not scene.error:
                try:
                    thumb = preview(scene, reference)
                    if thumb:
                        thumbnail = cache / f"{index}.jpg"
                        thumb.save(thumbnail, quality=84)
                except Exception as exc:
                    scene.findings.append(f"预览读取失败：{exc}")
                if registration:
                    try:
                        estimate_registration(scene, reference, windows, patches)
                    except Exception as exc:
                        scene.registration = "无法确认"
                        scene.findings.append(f"配准抽样失败：{exc}")
            events.put(("scene", (scene, thumbnail, index, len(scenes))))
    except Exception as exc:
        events.put(("error", str(exc)))
    finally:
        events.put(("done", None))


class TerraFrames:
    def __init__(self, root):
        self.root = root
        root.title("TerraFrames · 多时相影像检查")
        root.geometry("1100x740")
        root.minsize(980, 680)
        root.configure(bg=PAPER)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.events = Queue()
        self.stop = Event()
        self.cache = None
        self.reference = None
        self.chosen_reference = None
        self.items = []
        self.selected = set()
        self.focused = None
        self.page = 0
        self.running = False
        self.closing = False
        self.files = tk.StringVar(value="请选择包含 GeoTIFF 的文件夹")
        self.reference_label = tk.StringVar(value="参考影像：自动选择最早的有定位影像")
        self.summary = tk.StringVar(value="选好文件夹后，缩略图将按日期排列。")
        self.recursive = tk.BooleanVar()
        self.registration = tk.BooleanVar(value=True)
        self.page_label = tk.StringVar(value="—")
        self.title = tk.StringVar(value="影像详情")

        theme = ttk.Style(root)
        theme.theme_use("clam")
        theme.configure("TFrame", background=PAPER)
        theme.configure("TLabel", background=PAPER, foreground=INK, font=("Microsoft YaHei UI", 9))
        theme.configure("TCheckbutton", background=PAPER, foreground=INK, padding=3)
        theme.map("TCheckbutton", background=[("active", PAPER)])
        theme.configure("TButton", foreground=INK, padding=(10, 6), font=("Microsoft YaHei UI", 9))
        theme.configure("Primary.TButton", background=ACCENT, foreground=WHITE, borderwidth=0)
        theme.map("Primary.TButton", background=[("active", "#005544"), ("disabled", "#b9cbc1")],
                  foreground=[("disabled", "#334c45")])
        theme.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#d5e3da")

        frame = ttk.Frame(root, padding=(24, 16))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        ttk.Label(frame, text="TerraFrames", font=("Segoe UI Semibold", 23)).grid(row=0, sticky="w")
        ttk.Label(frame, text="多时相影像检查", foreground=MUTED).grid(row=1, sticky="w", pady=(0, 12))

        bar = ttk.Frame(frame)
        bar.grid(row=2, sticky="ew")
        bar.columnconfigure(0, weight=1)
        ttk.Entry(bar, textvariable=self.files, state="readonly").grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.folder_button = ttk.Button(bar, text="选择文件夹…", command=self.choose_folder)
        self.folder_button.grid(row=0, column=1, padx=(0, 7))
        self.reference_button = ttk.Button(bar, text="指定参考图…", command=self.choose_reference)
        self.reference_button.grid(row=0, column=2, padx=(0, 7))
        self.run_button = ttk.Button(bar, text="开始检查", command=self.start, style="Primary.TButton")
        self.run_button.grid(row=0, column=3)
        self.reference_button.bind("<Button-3>", lambda _: self.reset_reference())

        options = ttk.Frame(frame)
        options.grid(row=3, sticky="ew", pady=(6, 4))
        options.columnconfigure(0, weight=1)
        ttk.Label(options, textvariable=self.reference_label, foreground=MUTED, width=45).grid(row=0, column=0, sticky="w")
        self.reset_button = ttk.Button(options, text="自动参考", command=self.reset_reference)
        self.reset_button.grid(row=0, column=1, padx=(0, 8))
        self.subfolders = ttk.Checkbutton(options, text="含子文件夹", variable=self.recursive)
        self.subfolders.grid(row=0, column=2, padx=(0, 6))
        self.registration_check = ttk.Checkbutton(options, text="抽查内容配准", variable=self.registration)
        self.registration_check.grid(row=0, column=3)

        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=4, sticky="ew", pady=(1, 10))

        body = ttk.Frame(frame)
        body.grid(row=5, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.gallery = ttk.Frame(body)
        self.gallery.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        for col in range(3):
            self.gallery.columnconfigure(col, weight=1, uniform="gallery")
        for row in range(2):
            self.gallery.rowconfigure(row, weight=1, uniform="gallery")

        detail = tk.Frame(body, bg=WHITE, highlightthickness=1, highlightbackground="#c8d9d0")
        detail.grid(row=0, column=1, sticky="nsew")
        tk.Label(detail, textvariable=self.title, bg=WHITE, fg=INK, anchor="w",
                 wraplength=252, justify="left", font=("Microsoft YaHei UI", 11, "bold")).pack(
                     fill="x", padx=12, pady=(12, 6))
        text_box = tk.Frame(detail, bg=WHITE)
        text_box.pack(fill="both", expand=True, padx=(9, 4), pady=(0, 6))
        detail_scroll = ttk.Scrollbar(text_box)
        detail_scroll.pack(side="right", fill="y")
        self.details = tk.Text(text_box, width=27, height=11, borderwidth=0, highlightthickness=0,
                               bg=WHITE, fg=INK, wrap="word", padx=5, spacing3=5,
                               font=("Microsoft YaHei UI", 9), yscrollcommand=detail_scroll.set)
        self.details.pack(side="left", fill="both", expand=True)
        detail_scroll.configure(command=self.details.yview)
        self.pick_button = ttk.Button(detail, text="加入选片", command=self.toggle_selected, state="disabled")
        self.pick_button.pack(fill="x", padx=12, pady=(0, 6))
        self.zoom_button = ttk.Button(detail, text="放大预览…", command=self.zoom, state="disabled")
        self.zoom_button.pack(fill="x", padx=12, pady=(0, 12))

        footer = ttk.Frame(frame)
        footer.grid(row=6, sticky="ew", pady=(10, 0))
        footer.columnconfigure(3, weight=1)
        self.back = ttk.Button(footer, text="上一页", command=lambda: self.change_page(-1))
        self.back.grid(row=0, column=0)
        ttk.Label(footer, textvariable=self.page_label, padding=(9, 0)).grid(row=0, column=1)
        self.forward = ttk.Button(footer, text="下一页", command=lambda: self.change_page(1))
        self.forward.grid(row=0, column=2, padx=(0, 10))
        ttk.Label(footer, textvariable=self.summary, foreground=MUTED, wraplength=410).grid(row=0, column=3, sticky="w")
        self.export_button = ttk.Button(footer, text="导出检查表…", command=self.save, state="disabled")
        self.export_button.grid(row=0, column=4, padx=(7, 0))
        self.folder = None
        self.photos = []
        self.controls = (self.folder_button, self.reference_button, self.reset_button,
                         self.subfolders, self.registration_check)
        self.draw_gallery()
        self.show_detail()
        self.folder_button.focus_set()
        root.after(100, self.poll)

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            self.set_folder(Path(folder))

    def set_folder(self, folder):
        if self.folder != folder:
            self.reset_reference()
        self.folder = folder
        self.files.set(str(folder))
        self.summary.set("点「开始检查」扫描这个文件夹。")

    def choose_reference(self):
        path = filedialog.askopenfilename(parent=self.root, initialdir=str(self.folder or ""),
                                          filetypes=[("GeoTIFF", "*.tif *.tiff")])
        if path:
            self.chosen_reference = Path(path)
            self.reference_label.set(f"参考影像：{self.chosen_reference.name}")

    def reset_reference(self):
        self.chosen_reference = None
        self.reference_label.set("参考影像：自动选择最早的有定位影像")

    def start(self):
        if self.running:
            self.stop.set()
            self.run_button.configure(state="disabled")
            self.summary.set("正在停止，当前文件处理完成后结束。")
            return
        if self.folder is None:
            self.choose_folder()
            if self.folder is None:
                return
        if self.cache:
            self.cache.cleanup()
        self.cache = TemporaryDirectory(prefix="terraframes-")
        self.items.clear()
        self.selected.clear()
        self.focused = None
        self.reference = None
        self.page = 0
        self.stop.clear()
        self.running = True
        self.progress["value"] = 0
        self.summary.set("正在扫描…")
        self.run_button.configure(text="停止检查", state="normal")
        self.export_button.configure(state="disabled")
        for widget in self.controls:
            widget.configure(state="disabled")
        self.draw_gallery()
        self.show_detail()
        args = (self.folder, self.chosen_reference, self.recursive.get(), self.registration.get(),
                Path(self.cache.name), self.stop, self.events)
        Thread(target=scan, args=args, daemon=True).start()

    def poll(self):
        redraw = False
        try:
            for _ in range(100):
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.summary.set(value)
                elif kind == "reference":
                    self.reference = value
                    self.reference_label.set(f"本次参考：{value.path.name}")
                elif kind == "scene":
                    scene, thumbnail, done, total = value
                    redraw |= self.page * PAGE_SIZE <= len(self.items) < (self.page + 1) * PAGE_SIZE
                    self.items.append((scene, thumbnail))
                    self.progress["value"] = 100 * done / total
                    self.summary.set(f"检查与预览  {done}/{total}")
                    self.update_pages()
                elif kind == "error":
                    self.summary.set("检查未完成。")
                    if not self.closing:
                        messagebox.showerror("检查失败", value, parent=self.root)
                elif kind == "zoom":
                    scene, reference, image, error = value
                    if reference is self.reference and not self.closing:
                        if error:
                            messagebox.showerror("预览失败", error, parent=self.root)
                        elif image is not None:
                            self.show_zoom(scene, image)
                        self.zoom_button.configure(state="normal" if self.focused else "disabled")
                elif kind == "done":
                    self.running = False
                    if self.closing:
                        self.close()
                        return
                    self.run_button.configure(text="开始检查", state="normal")
                    for widget in self.controls:
                        widget.configure(state="normal")
                    if self.items:
                        self.export_button.configure(state="normal")
                        self.update_summary()
        except Empty:
            pass
        if redraw:
            self.draw_gallery()
        self.root.after(100, self.poll)

    def update_pages(self):
        pages = max(1, (len(self.items) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page_label.set(f"{self.page + 1} / {pages}" if self.items else "—")
        self.back.configure(state="normal" if self.page else "disabled")
        self.forward.configure(state="normal" if self.page + 1 < pages else "disabled")

    def draw_gallery(self):
        for child in self.gallery.winfo_children():
            child.destroy()
        self.photos.clear()
        self.update_pages()
        if not self.items:
            ttk.Label(self.gallery, text="打开一个影像文件夹", foreground=MUTED,
                      font=("Microsoft YaHei UI", 14)).grid(row=0, column=0, columnspan=3,
                                                            rowspan=2, sticky="n")
            return
        start = self.page * PAGE_SIZE
        for index, (scene, thumbnail) in enumerate(self.items[start:start + PAGE_SIZE]):
            focused = self.focused is scene
            card = tk.Frame(self.gallery, bg=WHITE, highlightthickness=2,
                            highlightbackground=ACCENT if focused else "#d3e0d8")
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=3, pady=3)
            card.columnconfigure(0, weight=1)
            box = tk.Frame(card, bg="#19242b", width=192, height=123)
            box.grid(row=0, column=0, sticky="ew")
            box.grid_propagate(False)
            box.columnconfigure(0, weight=1)
            box.rowconfigure(0, weight=1)
            button = tk.Button(box, text="无预览", bg="#19242b", fg=WHITE, relief="flat",
                               activebackground="#19242b", activeforeground=WHITE,
                               highlightthickness=1, highlightcolor=ACCENT,
                               command=lambda value=scene: self.focus(value))
            if thumbnail:
                with Image.open(thumbnail) as image:
                    image.thumbnail((203, 118))
                    photo = ImageTk.PhotoImage(image)
                self.photos.append(photo)
                button.configure(image=photo, text="")
            button.grid(sticky="nsew")
            status = "参考影像" if scene is self.reference else scene.grid
            if scene.footprint in ("部分覆盖", "无重叠") or scene.registration == "疑似内容平移":
                status += " · " + (scene.registration if scene.registration == "疑似内容平移" else scene.footprint)
            if scene.path in self.selected:
                status = "已选 · " + status
            tk.Label(card, text=scene.date or "日期未知", bg=WHITE, fg=INK,
                     font=("Segoe UI Semibold", 10), anchor="w").grid(sticky="ew", padx=8, pady=(5, 0))
            title = scene.path.name
            if len(title) > 27:
                title = title[:24] + "…"
            tk.Label(card, text=title, bg=WHITE, fg=MUTED, anchor="w").grid(sticky="ew", padx=8)
            tk.Label(card, text=status, bg=WHITE, fg=ACCENT if scene.grid == "格线对齐" else "#8a431e",
                     anchor="w", wraplength=187, justify="left").grid(sticky="ew", padx=8, pady=(1, 6))

    def change_page(self, offset):
        self.page += offset
        self.draw_gallery()

    def focus(self, scene):
        self.focused = scene
        self.show_detail()
        self.draw_gallery()

    def show_detail(self):
        scene = self.focused
        self.title.set(scene.path.name if scene else "影像详情")
        self.details.configure(state="normal")
        self.details.delete("1.0", tk.END)
        if scene:
            for name, value in describe(scene).items():
                if value:
                    self.details.insert(tk.END, f"{name}\n{value}\n\n")
        else:
            self.details.insert(tk.END, "点一张缩略图，查看像元、坐标系、覆盖范围和配准抽查。")
        self.details.configure(state="disabled")
        self.pick_button.configure(state="normal" if scene else "disabled",
                                   text="取消选片" if scene and scene.path in self.selected else "加入选片")
        self.zoom_button.configure(state="normal" if scene and not scene.error else "disabled")

    def zoom(self):
        scene, reference = self.focused, self.reference
        self.zoom_button.configure(state="disabled")

        def load():
            try:
                sample = inspect(scene.path)
                size = min(740, self.screen_limit)
                image = preview(sample, reference, size)
                self.events.put(("zoom", (scene, reference, image, sample.error or None)))
            except Exception as exc:
                self.events.put(("zoom", (scene, reference, None, str(exc))))

        self.screen_limit = max(180, min(self.root.winfo_screenwidth() - 80,
                                         self.root.winfo_screenheight() - 140))
        Thread(target=load, daemon=True).start()

    def show_zoom(self, scene, image):
        window = tk.Toplevel(self.root)
        window.title(scene.path.name)
        window.configure(bg="#19242b")
        photo = ImageTk.PhotoImage(image, master=window)
        view = tk.Label(window, image=photo, bg="#19242b", borderwidth=0)
        view.image = photo
        view.pack(padx=12, pady=12)

    def toggle_selected(self):
        path = self.focused.path
        if path in self.selected:
            self.selected.remove(path)
        else:
            self.selected.add(path)
        self.show_detail()
        self.draw_gallery()
        if not self.running:
            self.update_summary()

    def update_summary(self):
        aligned = sum(s.grid == "格线对齐" for s, _ in self.items)
        complete = sum(s.footprint == "范围一致" for s, _ in self.items)
        status = "已停止 · " if self.stop.is_set() else ""
        self.summary.set(f"{status}{len(self.items)} 张 · {aligned} 张格线对齐 · "
                         f"{complete} 张同范围 · 已选 {len(self.selected)} 张")

    def save(self):
        filename = filedialog.asksaveasfilename(parent=self.root, initialfile="terraframes-report.csv",
                                                defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if filename:
            try:
                output = Path(filename).resolve()
                export_csv(output, [s for s, _ in self.items], self.reference, self.selected)
            except (OSError, ValueError) as exc:
                messagebox.showerror("导出失败", str(exc), parent=self.root)
            else:
                self.summary.set(f"已导出 {output.name}")

    def close(self):
        if self.running:
            self.closing = True
            self.stop.set()
        else:
            if self.cache:
                self.cache.cleanup()
            self.root.destroy()


def main():
    root = tk.Tk()
    app = TerraFrames(root)
    if len(sys.argv) == 2 and Path(sys.argv[1]).is_dir():
        app.set_folder(Path(sys.argv[1]).resolve())
        app.start()
    root.mainloop()


if __name__ == "__main__":
    main()

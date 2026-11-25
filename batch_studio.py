"""Desktop controls for large GeoTIFF processing jobs."""
from collections import Counter
from datetime import datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, ttk

from batch_processing import execute_batch
from inventory import inventory
from temporal import monthly

OPERATIONS = ("快速盘点", "批量对齐", "月度合成")
RESAMPLING = {"最近邻": "nearest", "双线性": "bilinear", "平均": "average"}
VERSION = "5.0.0"


class BatchWorkspace(tk.Toplevel):
    def __init__(self, parent, folder=None, reference=None):
        super().__init__(parent)
        self.title("巨量遥感影像批量处理系统 · 批处理")
        self.geometry("930x650")
        self.minsize(760, 550)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.input = tk.StringVar(value=str(folder or ""))
        self.reference = tk.StringVar(value=str(reference or ""))
        self.destination = tk.StringVar()
        self.operation = tk.StringVar(value=OPERATIONS[1])
        self.workers = tk.StringVar(value="4")
        self.memory = tk.StringVar(value="512")
        self.resampling = tk.StringVar(value="最近邻")
        self.band = tk.StringVar(value="1")
        self.crop = tk.StringVar()
        self.compress = tk.BooleanVar(value=True)
        self.recursive = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="设置输入、参考影像和输出目录。")
        self.messages = SimpleQueue()
        self.stopped = Event()
        self.running = self.closing = False

        frame = ttk.Frame(self, padding=(20, 15))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(6, weight=1)
        ttk.Label(frame, text="批处理工作台", font=("Microsoft YaHei UI", 18, "bold")).grid(row=0, sticky="w")
        ttk.Label(frame, text="GeoTIFF · 盘点、统一格网和月度合成").grid(row=1, sticky="w", pady=(1, 11))

        paths = ttk.Frame(frame)
        paths.grid(row=2, sticky="ew")
        paths.columnconfigure(1, weight=1)
        for row, (label, variable, picker) in enumerate((
                ("影像目录", self.input, filedialog.askdirectory),
                ("参考影像", self.reference, filedialog.askopenfilename),
                ("输出目录", self.destination, filedialog.askdirectory))):
            ttk.Label(paths, text=label).grid(row=row, column=0, padx=(0, 10), sticky="w")
            ttk.Entry(paths, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Button(paths, text="选择…", command=lambda v=variable, p=picker: self.pick(v, p)).grid(
                row=row, column=2, padx=(8, 0), pady=3)

        settings = ttk.Frame(frame)
        settings.grid(row=3, sticky="ew", pady=(10, 3))
        for column, (label, variable, width, values) in enumerate((
                ("任务", self.operation, 12, OPERATIONS),
                ("并行数", self.workers, 5, None),
                ("工作内存 MiB", self.memory, 7, None),
                ("重采样", self.resampling, 9, tuple(RESAMPLING)),
                ("合成波段", self.band, 5, None))):
            ttk.Label(settings, text=label).grid(row=0, column=column*2, padx=(0, 4))
            control = (ttk.Combobox(settings, textvariable=variable, values=values, width=width, state="readonly")
                       if values else ttk.Entry(settings, textvariable=variable, width=width))
            control.grid(row=0, column=column*2+1, padx=(0, 11))

        options = ttk.Frame(frame)
        options.grid(row=4, sticky="ew", pady=(2, 8))
        ttk.Label(options, text="裁切（列, 行, 宽, 高）").pack(side="left")
        ttk.Entry(options, textvariable=self.crop, width=22).pack(side="left", padx=7)
        ttk.Checkbutton(options, text="含子文件夹", variable=self.recursive).pack(side="left", padx=5)
        ttk.Checkbutton(options, text="对齐输出压缩", variable=self.compress).pack(side="left", padx=5)
        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=5, sticky="ew", pady=(0, 8))

        results = ttk.Frame(frame)
        results.grid(row=6, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(results, columns=("item", "state", "detail"), show="headings")
        for column, title, width in (("item", "影像 / 月份", 200), ("state", "状态", 100), ("detail", "结果", 475)):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=70, stretch=column == "detail")
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(results, command=self.table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scroll.set)
        bottom = ttk.Frame(frame)
        bottom.grid(row=7, sticky="ew", pady=(9, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.status, wraplength=680).grid(row=0, column=0, sticky="w")
        self.run_button = ttk.Button(bottom, text="开始处理", style="Primary.TButton", command=self.start)
        self.run_button.grid(row=0, column=1, sticky="e")
        self.after(90, self.poll)

    def pick(self, variable, picker):
        if self.running:
            return
        path = picker(parent=self)
        if path:
            variable.set(path)

    def task(self):
        kind = self.operation.get()
        if not all(value.get().strip() for value in (self.input, self.reference, self.destination)):
            raise ValueError("请填写影像目录、参考影像和输出目录。")
        source, ref, out = (Path(value.get()).resolve() for value in (self.input, self.reference, self.destination))
        if not source.is_dir() or not ref.is_file() or source == out:
            raise ValueError("请确认影像目录和参考影像，并选择单独的输出目录。")
        workers = int(self.workers.get())
        recursive = self.recursive.get()
        stopped, events = self.stopped.is_set, self.messages

        def progress(done, total, name):
            events.put(("progress", (done, total, name)))

        if kind == OPERATIONS[0]:
            def inspect_all():
                out.mkdir(parents=True, exist_ok=True)
                file = out / datetime.now().strftime("inventory-%Y%m%d-%H%M%S-%f.csv")
                result = inventory(source, ref, file, workers, recursive, stopped, progress, exclude=out)
                return {**result, "operation": "inventory"}
            return inspect_all
        window = tuple(map(int, self.crop.get().replace("，", ",").split(","))) if self.crop.get().strip() else None
        if window and len(window) != 4:
            raise ValueError("裁切请按「列, 行, 宽, 高」填写四个整数。")
        options = dict(workers=workers, memory_mib=int(self.memory.get()),
                       resampling=RESAMPLING[self.resampling.get()], window=window,
                       recursive=recursive, stopped=stopped, progress=progress)
        if kind == OPERATIONS[1]:
            compression = "deflate" if self.compress.get() else "none"
            return lambda: execute_batch(source, ref, out, **options, compress=compression)
        band = int(self.band.get())
        if band < 1:
            raise ValueError("合成波段从 1 开始。")
        return lambda: monthly(source, ref, out, **options, band=band)

    def start(self):
        if self.running:
            self.stopped.set()
            self.status.set("正在停止，保留已经写完的产物。")
            self.run_button.configure(state="disabled")
            return
        try:
            action = self.task()
        except ValueError as error:
            self.status.set(str(error))
            return
        self.running = True
        self.stopped.clear()
        self.progress["value"] = 0
        self.table.delete(*self.table.get_children())
        self.status.set("正在准备任务…")
        self.run_button.configure(text="停止")

        def worker():
            try:
                self.messages.put(("done", action()))
            except Exception as error:
                self.messages.put(("error", str(error)))

        Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            for _ in range(100):
                kind, result = self.messages.get_nowait()
                if kind == "progress":
                    done, total, name = result
                    self.progress["value"] = 100*done/total
                    if not self.stopped.is_set():
                        self.status.set(f"{done}/{total} · {name}")
                    continue
                self.running = False
                self.run_button.configure(text="开始处理", state="normal")
                if self.closing:
                    self.destroy()
                    return
                if kind == "error":
                    self.status.set(result)
                else:
                    self.render(result)
        except Empty:
            pass
        self.after(90, self.poll)

    def render(self, report):
        seconds = report["seconds"]
        if report["operation"] == "inventory":
            for status, count in sorted(report["counts"].items()):
                self.table.insert("", "end", values=(status, str(count), report["output"]))
            self.status.set(f"盘点 {report['files']} 张 · {seconds:.2f} 秒 · CSV 已保存")
            return
        jobs = report["jobs"]
        counts = Counter(row["state"] for row in jobs)
        labels = {"completed": "完成", "failed": "失败", "cancelled": "停止", "reused": "已复用"}
        for row in sorted(jobs, key=lambda item: item["state"] != "failed")[:250]:
            name = row.get("month") or Path(row["source"]).name
            self.table.insert("", "end", values=(name, labels[row["state"]],
                              row.get("error") or row.get("output", "")))
        stats = " · ".join(f"{labels[state]} {count}" for state, count in sorted(counts.items()))
        stopped = "已停止 · " if report["cancelled"] else ""
        notes = " · 表内最多 250 条，详见完整报告" if len(jobs)>250 else " · 完整报告已保存"
        self.status.set(f"{stopped}{seconds:.2f} 秒 · 并行额度 {report['workers_used']} · {stats}{notes}")

    def close(self):
        if self.running:
            self.closing = True
            self.start()
        else:
            self.destroy()

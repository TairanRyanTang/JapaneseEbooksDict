import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import os
import sys


class WordExtractorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("日语生词分析工具 - 极速虚拟滚动版")
        self.root.geometry("1200x900")
        self.current_word_occurrences = []  # 当前选中单词的所有例句索引
        self.ctx_page_size = 15  # 每次加载 n 条例句
        self.ctx_current_page = 0  # 例句当前加载到第几页

        # 1. 资源路径处理
        def resource_path(relative_path):
            if hasattr(sys, '_MEIPASS'):
                return os.path.join(sys._MEIPASS, relative_path)
            return os.path.join(os.path.abspath("."), relative_path)

        icon_path = resource_path("app_icon.ico")
        if os.path.exists(icon_path):
            self.root.iconbitmap(icon_path)

        # 2. 初始化后端
        try:
            from processor import WordProcessor
            self.processor = WordProcessor()
        except ImportError:
            messagebox.showerror("错误", "找不到 processor.py")
            sys.exit(1)

        # 3. 虚拟滚动核心变量
        self.all_filtered_data = []  # 存放当前筛选后的所有单词索引数据
        self.visible_rows_count = 32  # 画面中显示的行数
        self.current_offset = 0  # 当前滚动到的起始位置
        self.last_filepath = None
        self.current_word_text = None
        self.sort_ascending = False

        # 字体与搜索
        self.font_size_var = tk.IntVar(value=12)
        self.search_var = tk.StringVar()
        self._search_timer = None

        # 词性配置
        self.pos_map = {
            '名詞': '名詞', '代名詞': '代名詞', '動詞': '動詞', '形容詞': '形容詞',
            '形状詞': '形容動詞', '副詞': '副詞', '連体詞': '連体詞', '接続詞': '接続詞',
            '助詞': '助詞', '助動詞': '助動詞'
        }
        defaults = {'名詞', '動詞', '形容詞', '形状詞', '副詞'}
        self.pos_vars = {k: tk.BooleanVar(value=(k in defaults)) for k in self.pos_map}

        self._setup_ui()
        self._bind_events()

    def _setup_ui(self):
        # --- 顶部工具栏 ---
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill=tk.X)

        ttk.Button(top_frame, text="📂 打开书籍", command=self.load_file).pack(side=tk.LEFT, padx=5)

        ttk.Label(top_frame, text="🔍 过滤:").pack(side=tk.LEFT, padx=(15, 2))
        self.search_var.trace_add("write", self._on_search_change)
        ttk.Entry(top_frame, textvariable=self.search_var, width=15).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(top_frame, textvariable=self.status_var, foreground="#666").pack(side=tk.LEFT, padx=20)

        self.progress = ttk.Progressbar(top_frame, orient=tk.HORIZONTAL, length=150, mode='determinate')
        self.progress.pack(side=tk.RIGHT, padx=10)

        # --- 词性筛选区 ---
        filter_frame = ttk.LabelFrame(self.root, text="词性过滤", padding=5)
        filter_frame.pack(fill=tk.X, padx=10, pady=5)
        for i, (k, v) in enumerate(self.pos_vars.items()):
            ttk.Checkbutton(filter_frame, text=self.pos_map[k], variable=v, command=self.fast_refresh).grid(row=0,
                                                                                                            column=i,
                                                                                                            padx=5)

        # --- 主面板 ---
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 左侧列表 (虚拟列表展示)
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=1)

        self.tree = ttk.Treeview(left_frame, columns=('word', 'pos', 'count'), show='headings', selectmode='browse')
        self.tree.heading('word', text='单词')
        self.tree.heading('pos', text='词性')
        self.tree.heading('count', text='频率', command=self.toggle_sort)

        self.tree.column('word', width=120)
        self.tree.column('pos', width=80, anchor='center')
        self.tree.column('count', width=60, anchor='center')

        # 重点：这个滚动条只负责改变 current_offset，不直接控制 Treeview
        self.vsb = ttk.Scrollbar(left_frame, orient="vertical", command=self._on_vsb_scroll)
        self.tree.configure(yscrollcommand=self._sync_vsb)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 右侧详情
        right_frame = ttk.Frame(paned, padding=15)
        paned.add(right_frame, weight=2)

        header = ttk.Frame(right_frame)
        header.pack(fill=tk.X, pady=(0, 10))
        self.lbl_word = ttk.Label(header, text="请选择单词", font=("Meiryo UI", 24, "bold"))
        self.lbl_word.pack(side=tk.LEFT)
        ttk.Button(header, text="🔊", width=3, command=self.speak_word).pack(side=tk.LEFT, padx=10)
        ttk.Button(header, text="✅ 标记为认识", command=self.mark_as_known).pack(side=tk.RIGHT)

        ttk.Label(right_frame, text="📖 释义 (由 SQLite 缓存驱动):").pack(anchor=tk.W)
        self.txt_def = tk.Text(right_frame, height=6, wrap=tk.WORD, relief="flat", bg="#fcfcfc", padx=10, pady=10)
        self.txt_def.pack(fill=tk.X, pady=(5, 15))

        ttk.Label(right_frame, text="🎧 上下文语境 (动态加载):").pack(anchor=tk.W)

        # 使用自定义 Frame 包裹 Text 和加载按钮
        ctx_container = ttk.Frame(right_frame)
        ctx_container.pack(fill=tk.BOTH, expand=True)

        self.txt_context = tk.Text(ctx_container, wrap=tk.WORD, relief="flat", padx=10, pady=10)
        self.txt_context.pack(fill=tk.BOTH, expand=True)
        self.txt_context.tag_config("target", foreground="#E63946", font=("Meiryo UI", 12, "bold"))
        self.txt_context.tag_config("index", foreground="#999999")

        # “加载更多”按钮
        self.btn_load_more_ctx = ttk.Button(ctx_container, text="加载更多例句...", command=self.load_more_context)
        self.btn_load_more_ctx.pack(fill=tk.X)

    # --- 虚拟滚动逻辑 ---

    def _bind_events(self):
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        # 监听滚轮
        self.tree.bind("<MouseWheel>", self._on_mouse_wheel)

    def _on_mouse_wheel(self, event):
        delta = -1 if event.delta > 0 else 1
        new_offset = max(0, min(self.current_offset + delta, len(self.all_filtered_data) - self.visible_rows_count))
        if new_offset != self.current_offset:
            self.current_offset = new_offset
            self._update_tree_display()
        return "break"

    def _on_vsb_scroll(self, *args):
        # 如果用户直接拖动滚动条
        if args[0] == "moveto":
            percent = float(args[1])
            self.current_offset = int(percent * (len(self.all_filtered_data) - self.visible_rows_count))
            self._update_tree_display()
        elif args[0] == "scroll":
            delta = int(args[1])
            self.current_offset = max(0, min(self.current_offset + delta,
                                             len(self.all_filtered_data) - self.visible_rows_count))
            self._update_tree_display()

    def _sync_vsb(self, low, high):
        # 伪造滚动条长度，让它看起来像是带了万行数据
        total = len(self.all_filtered_data)
        if total <= self.visible_rows_count:
            self.vsb.set(0, 1)
        else:
            low = self.current_offset / total
            high = (self.current_offset + self.visible_rows_count) / total
            self.vsb.set(low, high)

    def _update_tree_display(self):
        """核心：根据 offset 只刷新可见区域的内容"""
        # 清除旧行
        for item in self.tree.get_children():
            self.tree.delete(item)

        # 填充新行
        start = self.current_offset
        end = start + self.visible_rows_count
        chunk = self.all_filtered_data[start:end]

        for item in chunk:
            self.tree.insert('', tk.END,
                             values=(item['word'], self.pos_map.get(item['pos'], item['pos']), item['count']))

        self._sync_vsb(0, 0)  # 触发滚动条位置更新

    # --- 后端交互与业务逻辑 ---

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("电子书", "*.epub *.pdf")])
        if not path: return
        self.last_filepath = path
        self.status_var.set("⏳ 正在分析文本并读取缓存...")
        self.progress['value'] = 0

        def run():
            try:
                # analyze_file 现在会优先加载 .pkl 临时文件
                self.processor.analyze_file(path,
                                            lambda v: self.root.after(0, lambda: self.progress.configure(value=v)))
                self.root.after(0, self.fast_refresh)
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda m=msg: messagebox.showerror("分析失败", m))

        threading.Thread(target=run, daemon=True).start()

    def fast_refresh(self):
        """实时响应筛选条件，不卡顿"""
        if not self.last_filepath: return

        query = self.search_var.get().strip().lower()
        selected_pos = {k for k, v in self.pos_vars.items() if v.get()}

        # 1. 从后端获取过滤后的列表
        full_list = self.processor.apply_filters(selected_pos)

        # 2. 搜索过滤
        if query:
            full_list = [x for x in full_list if query in x['word'].lower()]

        # 3. 排序
        full_list.sort(key=lambda x: x['count'], reverse=not self.sort_ascending)

        self.all_filtered_data = full_list
        self.current_offset = 0
        self.status_var.set(f"共发现 {len(full_list)} 个生词")
        self._update_tree_display()

    def on_select(self, event):
        sel = self.tree.selection()
        if not sel: return

        # 获取当前选中的数据
        # 注意：因为是虚拟列表，数据要从 all_filtered_data 里根据 offset 找
        idx_in_tree = self.tree.index(sel[0])
        actual_idx = self.current_offset + idx_in_tree
        item_data = self.all_filtered_data[actual_idx]

        word = item_data['word']
        if word == self.current_word_text: return
        self.current_word_text = word

        # 1. 标题和释义
        self.lbl_word.config(text=word)
        self.txt_def.delete(1.0, tk.END)
        self.txt_def.insert(tk.END, "检索中...")
        threading.Thread(target=lambda: self._async_fetch_def(word), daemon=True).start()

        # 2. 重置例句分页
        self.txt_context.delete(1.0, tk.END)
        self.current_word_occurrences = item_data['occurrences']
        self.ctx_current_page = 0
        self.load_more_context()  # 加载第一页

    def load_more_context(self):
        """动态加载下一页例句"""
        start = self.ctx_current_page * self.ctx_page_size
        end = start + self.ctx_page_size

        chunk = self.current_word_occurrences[start:end]
        if not chunk:
            self.btn_load_more_ctx.config(state="disabled", text="已显示全部例句")
            return

        self.btn_load_more_ctx.config(state="normal",
                                      text=f"加载更多 ({len(self.current_word_occurrences) - end} 条剩余)...")

        for i, occ in enumerate(chunk, start=start + 1):
            sent = self.processor.current_sentences[occ['sent_idx']]
            s, e = occ['start'], occ['end']

            # 插入带索引的文本
            self.txt_context.insert(tk.END, f"[{i}] ", "index")
            self.txt_context.insert(tk.END, sent[:s])
            self.txt_context.insert(tk.END, sent[s:e], "target")
            self.txt_context.insert(tk.END, sent[e:] + "\n\n")

        self.ctx_current_page += 1

        # 如果后面没数据了，禁用按钮
        if end >= len(self.current_word_occurrences):
            self.btn_load_more_ctx.config(state="disabled", text="已显示全部例句")

    def _async_fetch_def(self, word):
        defi = self.processor.fetch_word_definition(word)
        self.root.after(0, lambda: self._update_def_ui(defi))

    def _update_def_ui(self, content):
        self.txt_def.delete(1.0, tk.END)
        self.txt_def.insert(tk.END, content)

    def _display_def(self, content):
        self.txt_def.delete(1.0, tk.END)
        self.txt_def.insert(tk.END, content)

    def _on_search_change(self, *args):
        if self._search_timer: self.root.after_cancel(self._search_timer)
        self._search_timer = self.root.after(400, self.fast_refresh)

    def mark_as_known(self):
        if self.current_word_text:
            self.processor.add_known_word(self.current_word_text)
            self.fast_refresh()  # 刷新后单词会自动消失

    def toggle_sort(self):
        self.sort_ascending = not self.sort_ascending
        self.fast_refresh()

    def speak_word(self):
        if self.current_word_text:
            self.processor.play_audio(self.current_word_text)


if __name__ == "__main__":
    root = tk.Tk()
    app = WordExtractorApp(root)
    root.mainloop()
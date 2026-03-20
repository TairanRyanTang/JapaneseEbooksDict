import os
import re
import csv
import time
import io
import pickle
import hashlib
import sqlite3
import threading
import warnings
import asyncio
from concurrent.futures import ThreadPoolExecutor

# 屏蔽非关键警告
warnings.filterwarnings("ignore", category=UserWarning)


class WordProcessor:
    # 预编译正则：加速清洗
    RE_WHITESPACE = re.compile(r'\s+')
    RE_SENTENCE_SPLIT = re.compile(r'([。？！])')
    RE_CLEAN_NUM_SYM = re.compile(r'^[0-9０-９\.,，．\-\+\s%％a-zA-Z]+$')

    def __init__(self):
        # 路径配置
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = os.path.join(self.base_dir, "cache_temp")
        os.makedirs(self.cache_dir, exist_ok=True)

        # 熟词本路径
        self.known_words_file = os.path.join(self.base_dir, "known_words.txt")
        self.known_words = self._load_known_words()

        # 1. 初始化 SQLite 释义数据库
        self.db_path = os.path.join(self.cache_dir, "definitions.db")
        self._init_db()

        # 2. 运行时容器
        self.current_sentences = []
        self.current_raw_stats = {}  # 存储: {word: {'pos': pos, 'count': n, 'occurrences': []}}

        # 3. 延迟加载对象
        self._tokenizer_obj = None
        self._mode = None

    def _init_db(self):
        """初始化释义缓存数据库"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS dict (word TEXT PRIMARY KEY, def TEXT)")

    @property
    def tokenizer(self):
        """懒加载分词器，保证程序秒开"""
        if self._tokenizer_obj is None:
            from sudachipy import dictionary, tokenizer
            self._tokenizer_obj = dictionary.Dictionary().create()
            self._mode = tokenizer.Tokenizer.SplitMode.B
        return self._tokenizer_obj

    def _get_file_hash(self, filepath):
        """生成文件唯一指纹"""
        stat = os.stat(filepath)
        id_str = f"{filepath}_{stat.st_mtime}_{stat.st_size}"
        return hashlib.md5(id_str.encode('utf-8')).hexdigest()

    # --- 核心方法 1: 分析文件 (带持久化缓存) ---
    def analyze_file(self, file_path, progress_callback=None):
        file_hash = self._get_file_hash(file_path)
        cache_file = os.path.join(self.cache_dir, f"{file_hash}.pkl")

        # 尝试从磁盘加载缓存
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'rb') as f:
                    self.current_sentences, self.current_raw_stats = pickle.load(f)
                if progress_callback: progress_callback(100)
                return True
            except:
                pass

        # 解析文件内容
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".epub":
            sentences = self._extract_epub(file_path)
        elif ext == ".pdf":
            sentences = self._extract_pdf(file_path, progress_callback)
        else:
            raise ValueError("不支持的格式")

        self.current_sentences = sentences
        raw_stats = {}
        tk = self.tokenizer
        mode = self._mode
        total = len(sentences)

        # 执行分词统计
        for idx, sent in enumerate(sentences):
            if not sent.strip(): continue
            # 分词
            for m in self.tokenizer.tokenize(sent, self._mode):
                word = m.normalized_form()
                if self.RE_CLEAN_NUM_SYM.match(word): continue

                pos = m.part_of_speech()[0]
                if word not in raw_stats:
                    raw_stats[word] = {'pos': pos, 'count': 0, 'occurrences': []}

                raw_stats[word]['count'] += 1
                # --- 优化点：移除数量限制，只存储极小的索引对象 ---
                raw_stats[word]['occurrences'].append({
                    'sent_idx': idx,
                    'start': m.begin(),
                    'end': m.end()
                })

        self.current_raw_stats = raw_stats
        # 保存缓存
        with open(cache_file, 'wb') as f:
            pickle.dump((self.current_sentences, self.current_raw_stats), f)
        return True

    # --- 核心方法 2: 过滤逻辑 (内存操作，极快) ---
    def apply_filters(self, selected_pos_set):
        if not self.current_raw_stats: return []

        results = []
        for word, data in self.current_raw_stats.items():
            if word in self.known_words: continue
            if data['pos'] not in selected_pos_set: continue

            results.append({
                'word': word,
                'pos': data['pos'],
                'count': data['count'],
                'occurrences': data['occurrences']
            })
        return results

    # --- 核心方法 3: 释义缓存驱动 ---
    def fetch_word_definition(self, word):
        """SQLite 二级缓存逻辑"""
        # 1. 查库
        try:
            with sqlite3.connect(self.db_path) as conn:
                res = conn.execute("SELECT def FROM dict WHERE word=?", (word,)).fetchone()
                if res: return res[0]
        except:
            pass

        # 2. 查网络
        from weblio_scraper import get_weblio_definition
        definition = get_weblio_definition(word)

        # 3. 入库
        if definition and "网络查询失败" not in definition:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("INSERT OR REPLACE INTO dict VALUES (?, ?)", (word, definition))
            except:
                pass

        return definition

    # --- 文件解析逻辑 (懒加载) ---
    def _extract_epub(self, path):
        """EPUB 解码优化版：支持自动编码检测与错误容错"""
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup

        # 针对部分 EPUB 编码不规范，增加错误处理
        book = epub.read_epub(path, options={'ignore_ncx': True})
        sents = []

        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            try:
                # 核心优化：先尝试 utf-8，失败则回退到容错模式或 Latin-1 (常用语字节流强制转换)
                raw_content = item.get_content()
                try:
                    content = raw_content.decode('utf-8')
                except UnicodeDecodeError:
                    # 如果 UTF-8 失败，尝试用 'cp932'(日语Shift-JIS) 或 'replace' 强制解码
                    content = raw_content.decode('shift-jis', errors='replace')

                soup = BeautifulSoup(content, 'html.parser')

                # 移除注音（Ruby标签），只保留主体文字，防止分词混乱
                for r in soup.find_all('ruby'):
                    rt = r.find('rt')
                    if rt: rt.extract()

                # 清洗文本
                text = soup.get_text()
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                sents.extend(lines)
            except Exception as e:
                print(f"[警告] 解码章节失败: {str(e)}")
                continue
        return sents

    def _extract_pdf(self, path, progress_callback):
        """PDF 解码优化版：处理特殊字符替换"""
        import pdfplumber
        sents = []
        try:
            with pdfplumber.open(path) as pdf:
                total = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    if progress_callback and i % 5 == 0:
                        progress_callback((i / total) * 20)

                    # 使用 pdfplumber 提取时，部分PDF内部编码表缺失会导致出现 REPLACEMENT CHARACTER
                    text = page.extract_text()
                    if text:
                        # 1. 预处理：替换常见的 PDF 提取乱码或控制字符
                        text = text.replace('\u0000', '')  # 移除空字符
                        text = self.RE_WHITESPACE.sub(' ', text)  # 统一空白符

                        # 2. 规范化日语全角符号（防止标点符号解码不统一导致的分句失败）
                        text = text.replace('．', '。').replace('．', '。')

                        parts = self.RE_SENTENCE_SPLIT.split(text)
                        for j in range(0, len(parts) - 1, 2):
                            combined = parts[j] + parts[j + 1]
                            if len(combined.strip()) > 1:
                                sents.append(combined.strip())
        except Exception as e:
            print(f"[错误] PDF读取失败: {str(e)}")
        return sents

    # --- 语音与辅助 ---
    def play_audio(self, text):
        threading.Thread(target=self._run_tts, args=(text,), daemon=True).start()

    def _run_tts(self, text):
        import edge_tts
        import pygame
        import asyncio
        if not pygame.mixer.get_init(): pygame.mixer.init()

        async def amain():
            comm = edge_tts.Communicate(text, "ja-JP-NanamiNeural")
            data = b""
            async for chunk in comm.stream():
                if chunk["type"] == "audio": data += chunk["data"]

            f = io.BytesIO(data)
            pygame.mixer.music.load(f)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy(): await asyncio.sleep(0.1)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(amain())

    def _load_known_words(self):
        if os.path.exists(self.known_words_file):
            with open(self.known_words_file, 'r', encoding='utf-8') as f:
                return {line.strip() for line in f if line.strip()}
        return set()

    def add_known_word(self, word):
        if word not in self.known_words:
            self.known_words.add(word)
            with open(self.known_words_file, 'a', encoding='utf-8') as f:
                f.write(f"{word}\n")
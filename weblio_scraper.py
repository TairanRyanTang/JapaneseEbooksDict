import requests
from bs4 import BeautifulSoup
import re


def get_weblio_definition(word):
    url = f"https://www.weblio.jp/content/{word}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36..."}

    try:
        response = requests.get(url, headers=headers, timeout=8)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        # 1. 查找所有解释块
        kiji_parts = soup.find_all(class_='kiji')
        if not kiji_parts:
            kiji_parts = soup.find_all(class_='NetDicBody')

        valid_blocks = []
        for part in kiji_parts:
            # 关键修复：不再使用 separator="\n"，防止单词断行
            text = part.get_text(strip=True)

            # 2. 过滤掉明显的单字汉字词典
            # 如果开头就是 [汉字]読み方： 这种，通常不是我们要的释义
            if text.startswith(word) and "読み方：" in text and len(word) == 1:
                continue

            # 3. 格式化：手动给编号和特殊符号加换行，让排版美观
            text = re.sub(r'([①②③④⑤１２３４５])', r'\n\1', text)
            text = re.sub(r'(［[名代動形副]］)', r'\n\1', text)

            valid_blocks.append(text)

        if not valid_blocks: return "未找到详细释义"

        # 4. 排序：把包含“辞典”特征的块放在前面
        valid_blocks.sort(key=lambda x: 1 if any(m in x for m in ["１", "意味", "［"]) else 0, reverse=True)

        return "\n\n---\n\n".join(valid_blocks[:2])  # 只取前两个最重要的

    except Exception as e:
        return f"网络查询失败: {e}"

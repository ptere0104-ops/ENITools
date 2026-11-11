# -*- coding: utf-8 -*-
"""抓取 X 上 ENI 官方账号（@ENI__Official）的公开时间线，只保留官方本人发的内容。

原理：x.com 的未登录页面（普通浏览器 UA、不带 cookie）会把当前可见推文以
JSON 内联在 HTML 里，键名带转义（形如 \\"full_text\\":\\"...\\"）。以 full_text
为锚点向前回溯，靠 TweetResults 的 base64 节点 id 认出这一条推文的真实 id，
再按作者过滤 —— 别人买的推广帖会被自动剔除。发布时间不依赖页面字段，
直接用推文 id 的 snowflake 规则算出。

不需要登录、不需要 API key、不需要无头浏览器。

用法：
    python fetch_news.py                     # 抓取并写 news-eni.json
    python fetch_news.py --dry               # 只打印不落盘
    python fetch_news.py --file xpage.html   # 解析本地已保存的页面
"""
import base64
import io
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

HANDLE = 'ENI__Official'
SOURCE_URL = 'https://x.com/%s' % HANDLE
OUT_PATH = 'news-eni.json'
MAX_ITEMS = 12
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

BQ = chr(92) + chr(34)
K_TEXT = BQ + 'full_text' + BQ + ':' + BQ
K_REST = BQ + 'rest_id' + BQ + ':' + BQ
K_AUTH = BQ + 'screen_name' + BQ + ':' + BQ
RE_TWEET_NODE = re.compile(r'VHdlZXRSZXN1bHRzOj[A-Za-z0-9+/]{6,}=*')


def fetch_html(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': UA,
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
                'Cache-Control': 'no-cache',
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode('utf-8', 'ignore')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 + i * 4)
    raise RuntimeError('fetch failed: %r' % last)


def read_value(text, start):
    """读取一个转义 JSON 字符串值，遇到未转义的 \\" 结束。"""
    buf = []
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == chr(92):
            nxt = text[i + 1] if i + 1 < n else ''
            if nxt == '"':
                return ''.join(buf), i + 2
            if nxt:
                buf.append(c)
                buf.append(nxt)
                i += 2
                continue
            buf.append(c)
            i += 1
            continue
        if c == '"':
            return ''.join(buf), i + 1
        buf.append(c)
        i += 1
    return ''.join(buf), i


def unescape(raw):
    try:
        return json.loads('"%s"' % raw)
    except Exception:  # noqa: BLE001
        return raw.replace('\\n', ' ').replace('\\"', '"').replace('\\/', '/')


def clean(text):
    text = text.replace('\r', '')
    text = text.replace('\\n', '\n').replace('\\t', ' ').replace('\\"', '"').replace('\\/', '/')
    text = re.sub(r'https://t\.co/\S+', '', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def values(text, key, limit=40):
    out = []
    pos = 0
    while len(out) < limit:
        i = text.find(key, pos)
        if i < 0:
            break
        v, end = read_value(text, i + len(key))
        out.append((v, i))
        pos = max(end, i + 1)
    return out


def tweet_id_from_node(head):
    """从 TweetResults 的 base64 节点 id 解出真实推文 id（取最后一个）。"""
    found = []
    for m in RE_TWEET_NODE.finditer(head):
        tok = m.group(0)
        tok += '=' * (-len(tok) % 4)
        try:
            dec = base64.b64decode(tok).decode('utf-8', 'ignore')
        except Exception:  # noqa: BLE001
            continue
        parts = dec.split(':')
        if len(parts) == 2 and parts[1].isdigit():
            found.append(parts[1])
    return found[-1] if found else ''


def snowflake_time(tid):
    try:
        ms = (int(tid) >> 22) + 1288834974657
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:  # noqa: BLE001
        return ''


def parse(html):
    items = []
    for v, i in values(html, K_TEXT):
        head = html[max(0, i - 7000):i]
        auths = values(head, K_AUTH, 30)
        if not auths:
            continue
        tid = tweet_id_from_node(head)
        if not tid:
            ids = values(head, K_REST, 30)
            tid = unescape(ids[-1][0]) if ids else ''
        if not tid:
            continue
        items.append({
            'id': tid,
            'author': unescape(auths[-1][0]),
            'time': snowflake_time(tid),
            'text': clean(unescape(v)),
        })
    return items


def main():
    args = sys.argv[1:]
    dry = '--dry' in args
    local = None
    if '--file' in args:
        local = args[args.index('--file') + 1]

    if local:
        html = io.open(local, encoding='utf-8', errors='ignore').read()
    else:
        try:
            html = fetch_html(SOURCE_URL)
        except Exception as e:  # noqa: BLE001
            # 数据中心 IP 常被 X 拦（HTTP 403）；此时保留仓库里已有的快照，不算失败。
            print('source unavailable, keep existing snapshot: %r' % e)
            return

    raw = parse(html)
    keep = []
    for it in raw:
        if it['author'] != HANDLE:
            continue          # 广告 / 推广帖 / 转帖里的第三方，全部丢掉
        if len(it['text']) < 10 or not it['time']:
            continue
        keep.append({
            'id': it['id'],
            'url': 'https://x.com/%s/status/%s' % (HANDLE, it['id']),
            'time': it['time'],
            'text': it['text'][:320],
        })

    uniq = {}
    for it in keep:
        uniq[it['id']] = it
    keep = sorted(uniq.values(), key=lambda x: x['time'], reverse=True)[:MAX_ITEMS]

    out = {
        'source': SOURCE_URL,
        'handle': HANDLE,
        'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'count': len(keep),
        'items': keep,
    }

    print('raw=%d kept=%d' % (len(raw), len(keep)))
    for it in keep:
        print(' -', it['time'], '|', it['text'][:100].replace('\n', ' '))

    if not keep:
        # 没解析出官方帖时不要覆盖旧快照（避免把资讯清空）。
        print('no official items parsed, keep existing snapshot')
        return

    if not dry:
        with io.open(OUT_PATH, 'w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(out, ensure_ascii=False, indent=1))
        print('written', OUT_PATH)


if __name__ == '__main__':
    main()

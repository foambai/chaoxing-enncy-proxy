# -*- coding: utf-8 -*-
"""
言溪题库本地融合代理 (enncy_proxy.py)

作用: 把 chaoxing.exe 的 TikuAdapter 通道桥接到言溪题库 (tk.enncy.cn),
请求使用与官网 dashboard "AI搜题" 生成的题库配置相同的参数格式
(token + title + options + type), 因此:
  1. dashboard 里开启的 AI 解答(智能模式: 先题库后AI兜底)对文字题自动生效;
  2. 所有查询都消耗本 token, 在 dashboard 用量/查询记录里可以看到;
  3. 题库/AI 返回的答案会被规范化成脚本认可的格式(选项原文/正确/错误),
     避免 "答案类型与题目类型不符, 已舍弃" 的问题。

可选: 在 config.ini 的 [tiku] 里配置 llm_endpoint / llm_key / llm_model
(OpenAI格式、支持读图的模型), 题库没有且带图片的题目会自动转给大模型兜底。
留空则不带图题目维持原样(查不到就跳过)。

用法: 双击 start_proxy.bat, 或 `python enncy_proxy.py`, 保持窗口开启,
再照常运行 chaoxing.exe (config.ini 里 provider 需为 TikuAdapter)。
"""

import base64
import configparser
import difflib
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.ini')
CACHE_PATH = os.path.join(BASE_DIR, 'enncy_cache.json')
LOG_PATH = os.path.join(BASE_DIR, 'enncy_proxy.log')

ENNCY_API = 'https://tk.enncy.cn/query'
PORT = 8765
ENNCY_TIMEOUT = 25
LLM_TIMEOUT = 90
NEGATIVE_TTL = 7 * 24 * 3600  # 题库+大模型都答不出的题, 7天内不再重复请求

NO_ANSWER_MARKS = ('非常抱歉', '没有该题', '搜不到答案')
_TRUE_WORDS = {'正确', '对', '√', '是', 'true', 't', 'yes', 'y', '对的'}
_FALSE_WORDS = {'错误', '错', '×', 'x', '否', '不对', '不正确', 'false', 'f', 'no', 'n'}

# 与 chaoxing.exe 内 check_answer/cut 的切割符保持一致: exe 会把选择类答案按这些字符
# 切开, 切开后不是恰好一段就被判 "答案类型与题目类型不符" 丢弃, 所以必须预先剥除
CUT_CHARS = set('\n\r\t,，|#*-+_@~/\\.& 、')


def strip_cut_chars(s):
    return ''.join(ch for ch in str(s or '') if ch not in CUT_CHARS)

_log_lock = threading.Lock()


def log(level, msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


def http_get(url, timeout=ENNCY_TIMEOUT, referer=None):
    """带SSL验证失败回退的GET, 返回bytes"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except ssl.SSLCertVerificationError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()


def http_post_json(url, payload, timeout=LLM_TIMEOUT):
    data = json.dumps(payload).encode('utf-8')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Content-Type': 'application/json',
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except ssl.SSLCertVerificationError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode('utf-8'))


class ProxyState:
    def __init__(self):
        self.tokens = []
        self.token_index = 0
        self.true_target = '正确'
        self.false_target = '错误'
        self.llm_endpoint = ''
        self.llm_key = ''
        self.llm_model = ''
        self.llm_min_interval = 2.0
        self._last_llm_ts = 0.0
        self._llm_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache = {}
        self.load_config()
        self.load_cache()

    # ---------- 配置 ----------
    def load_config(self):
        cp = configparser.ConfigParser()
        cp.read(CONFIG_PATH, encoding='utf8')
        sec = cp['tiku'] if 'tiku' in cp else {}
        tokens = [t.strip() for t in str(sec.get('tokens', '')).split(',') if t.strip()]
        if not tokens:
            log('ERROR', 'config.ini [tiku] tokens 未配置, 无法查询言溪题库')
        self.tokens = tokens
        true_list = [t.strip() for t in str(sec.get('true_list', '')).split(',') if t.strip()]
        false_list = [t.strip() for t in str(sec.get('false_list', '')).split(',') if t.strip()]
        if true_list:
            self.true_target = true_list[0]
        if false_list:
            self.false_target = false_list[0]
        self.llm_endpoint = str(sec.get('llm_endpoint', '')).strip().rstrip('/')
        self.llm_key = str(sec.get('llm_key', '')).strip()
        self.llm_model = str(sec.get('llm_model', '')).strip()
        try:
            self.llm_min_interval = float(sec.get('llm_min_interval_seconds', 2))
        except (TypeError, ValueError):
            self.llm_min_interval = 2.0
        if self.llm_enabled():
            log('INFO', f'视觉大模型兜底已启用: {self.llm_model}')
        else:
            log('INFO', '视觉大模型兜底未配置(llm_endpoint/llm_key/llm_model 为空), 带图题将跳过')

    def llm_enabled(self):
        return bool(self.llm_endpoint and self.llm_key and self.llm_model)

    # ---------- 缓存 ----------
    def load_cache(self):
        try:
            with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                self._cache = json.load(f)
        except (OSError, ValueError):
            self._cache = {}

    def cache_key(self, qtype, question, options):
        raw = f'{qtype}|{question}|{"\n".join(options)}'
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    def cache_get(self, key):
        with self._cache_lock:
            item = self._cache.get(key)
        if not item:
            return None, False
        if item.get('answer') is None and time.time() - item.get('ts', 0) > NEGATIVE_TTL:
            return None, False
        return item.get('answer'), item.get('answer') is not None

    def cache_set(self, key, answer):
        with self._cache_lock:
            self._cache[key] = {'answer': answer, 'ts': time.time()}
            try:
                with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                    json.dump(self._cache, f, ensure_ascii=False)
            except OSError as e:
                log('WARN', f'缓存写入失败: {e}')

    # ---------- 言溪查询 ----------
    def current_token(self):
        with self._token_lock:
            return self.tokens[self.token_index % len(self.tokens)] if self.tokens else ''

    def rotate_token(self):
        with self._token_lock:
            if self.token_index == len(self.tokens) - 1:
                log('ERROR', '所有TOKEN均次数不足, 请到 dashboard 更换/充值后重启代理')
            self.token_index = (self.token_index + 1) % max(len(self.tokens), 1)

    def query_enncy(self, question, options, qtype):
        """按 dashboard 配置的同款参数格式查询, 返回答案文本或None"""
        if not self.tokens:
            return None
        params = {
            'token': self.current_token(),
            'title': question,
            'options': '\n'.join(options),
            'type': qtype,
        }
        url = f'{ENNCY_API}?{urllib.parse.urlencode(params)}'
        try:
            body = http_get(url).decode('utf-8')
            res = json.loads(body)
        except Exception as e:
            log('ERROR', f'言溪请求异常: {e}')
            return None
        code = res.get('code')
        data = res.get('data') or {}
        answer = str(data.get('answer', '') or '')
        if code:
            if any(m in answer for m in NO_ANSWER_MARKS):
                return None
            times = data.get('times')
            ai = data.get('ai')
            log('INFO', f'言溪返回答案(来源: {"AI" if ai else "题库"}, 剩余次数: {times}): {answer[:60]}')
            return answer.strip()
        if '次数不足' in answer or '次数不足' in str(res.get('message', '')):
            log('INFO', 'TOKEN查询次数不足, 切换下一个token重试')
            self.rotate_token()
            return self.query_enncy(question, options, qtype) if len(self.tokens) > 1 else None
        log('INFO', f'言溪题库无此题: {str(res.get("message", ""))[:60]}')
        return None

    # ---------- 大模型兜底 ----------
    def query_llm(self, question, options, qtype, image_urls):
        if not self.llm_enabled():
            return None
        type_hint = {
            0: '这是一道单选题, 只回答正确选项的字母(如 A)。',
            1: '这是一道多选题, 只回答所有正确选项的字母, 不要分隔(如 ABD)。',
            2: '这是一道填空题, 直接简短回答答案, 多个空用英文分号;分隔。',
            3: '这是一道判断题, 只回答两个词之一: 正确 或 错误。',
        }.get(qtype, '直接简短回答题目答案。')
        text = f'{type_hint}\n题目: {question}'
        if options and qtype in (0, 1, 3):
            text += '\n选项:\n' + '\n'.join(f'{chr(65 + i)}. {o}' for i, o in enumerate(options))
        content = [{'type': 'text', 'text': text}]
        for u in image_urls[:4]:
            img = self.fetch_image_b64(u)
            if img:
                content.append({'type': 'image_url',
                                'image_url': {'url': f'data:image/jpeg;base64,{img}'}})
        url = self.llm_endpoint if self.llm_endpoint.endswith('/chat/completions') \
            else self.llm_endpoint + '/chat/completions'
        payload = {
            'model': self.llm_model,
            'messages': [{'role': 'user', 'content': content}],
            'temperature': 0,
        }
        headers_key = self.llm_key
        with self._llm_lock:
            wait = self.llm_min_interval - (time.time() - self._last_llm_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_llm_ts = time.time()
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json',
                         'Authorization': f'Bearer {headers_key}',
                         'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                    res = json.loads(resp.read().decode('utf-8'))
            except ssl.SSLCertVerificationError:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=LLM_TIMEOUT, context=ctx) as resp:
                    res = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            log('ERROR', f'大模型请求失败: {e}')
            return None
        try:
            msg = res['choices'][0]['message']['content']
            if isinstance(msg, list):
                msg = ''.join(p.get('text', '') for p in msg if isinstance(p, dict))
            answer = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', str(msg)).strip()
            log('INFO', f'大模型返回: {answer[:60]}')
            return answer or None
        except (KeyError, IndexError, TypeError):
            log('ERROR', f'大模型响应格式异常: {str(res)[:200]}')
            return None

    @staticmethod
    def fetch_image_b64(url):
        for referer in (None, 'http://i.chaoxing.com/', 'http://mooc1.chaoxing.com/'):
            try:
                data = http_get(url, timeout=15, referer=referer)
                if data[:6].startswith(b'<html') or b'403' in data[:200]:
                    continue
                return base64.b64encode(data).decode('ascii')
            except Exception:
                continue
        log('WARN', f'题目图片下载失败: {url[:80]}')
        return None


# ---------- 答案规范化 ----------
def norm(s):
    return re.sub(r'[\s。，、．.（）()：:；;，,？?“”"\'【】\[\]]', '', str(s or '')).lower()


def letter_indexes(ans, n_options):
    if re.fullmatch(r'[A-Za-z]{1,6}', ans):
        idxs = [ord(c.upper()) - 65 for c in ans]
        if all(0 <= i < n_options for i in idxs):
            return idxs
    return None


def match_option(ans, options):
    """把一段答案文本匹配到选项列表, 返回选项下标或None"""
    a = norm(ans)
    if not a:
        return None
    for i, o in enumerate(options):
        if norm(o) == a:
            return i
    for i, o in enumerate(options):
        no = norm(o)
        if len(a) >= 2 and no and (no in a or a in no):
            return i
    best, best_r = None, 0.0
    for i, o in enumerate(options):
        r = difflib.SequenceMatcher(None, a, norm(o)).ratio()
        if r > best_r:
            best, best_r = i, r
    return best if best_r >= 0.55 else None


def normalize_choice(answer, options, multiple):
    """把题库/AI/大模型答案映射成选项原文列表"""
    answer = str(answer or '').strip()
    if not answer:
        return None
    idxs = letter_indexes(answer, len(options))
    if idxs is not None:
        picked = sorted(set(idxs))
        if multiple or len(picked) == 1:
            return [options[i] for i in picked]
    pieces = [p for p in re.split(r'[#\n\r;；]', answer) if p.strip()]
    matched = set()
    for p in pieces:
        i = match_option(p, options)
        if i is not None:
            matched.add(i)
        if multiple:
            # 题库可能用空格/无分隔把多个选项连在一起, 把该段里包含的所有选项都找出来
            np = norm(p)
            for j, o in enumerate(options):
                no = norm(o)
                if len(no) >= 4 and no in np:
                    matched.add(j)
    if not matched and len(pieces) == 1:
        for p in re.split(r'[，,、]', pieces[0]):
            i = match_option(p, options)
            if i is not None:
                matched.add(i)
    if not matched:
        i = match_option(answer, options)
        if i is not None:
            matched.add(i)
    if matched:
        return [options[i] for i in sorted(matched)]
    if not multiple:
        return [answer]
    return None


def normalize_judgement(answer, state):
    raw = str(answer or '').strip()
    a = norm(raw)
    if a in _TRUE_WORDS:
        return state.true_target
    if a in _FALSE_WORDS:
        return state.false_target
    # 题库可能返回 "(正确)原文:..." / "错误。原文:..." 等带解释的格式,
    # 剥掉包裹符号后按开头的判定词识别 (先否定词避免 不正确/不对 被误判)
    head = re.sub(r'^[\s()（）\[\]【】"\'“”‘’。.,，;；:：-]+', '', raw)
    low = head.lower()
    for w in _FALSE_WORDS:
        if w and low.startswith(w):
            return state.false_target
    for w in _TRUE_WORDS:
        if w and low.startswith(w):
            return state.true_target
    return None


def normalize_completion(answer):
    a = re.sub(r'^(参考)?答案[:：]\s*', '', str(answer or '').strip())
    a = re.sub(r'\s*分$', '', a)
    return a or None


class Handler(BaseHTTPRequestHandler):
    state = None  # type: ProxyState

    def log_message(self, fmt, *args):
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({'ok': True, 'service': 'enncy-proxy', 'llm': self.state.llm_enabled()})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                text = raw.decode('gbk', errors='replace')
            req = json.loads(text)
            question = str(req.get('question', ''))
            options = [str(o) for o in (req.get('options') or []) if str(o).strip()]
            qtype = int(req.get('type', 4))
        except Exception as e:
            self._send({'error': f'bad request: {e}'}, 400)
            return

        img_urls = re.findall(r'<img[^>]*src=["\']([^"\']+)["\']', question, re.I)
        clean_q = re.sub(r'<img[^>]*>', '', question, flags=re.I)
        clean_q = clean_q.replace('&nbsp;', ' ').replace('&amp;', '&')
        clean_q = re.sub(r'\s+', ' ', clean_q).strip()

        short_q = clean_q[:50]
        ckey = self.state.cache_key(qtype, clean_q, options)
        cached, hit = self.state.cache_get(ckey)
        if hit:
            answer_list = cached or None
            log('INFO', f'[缓存] {"命中" if answer_list else "无答案(负面缓存)"}: {short_q}')
        else:
            answer_list = None
            bank = self.state.query_enncy(clean_q, options, qtype)
            if bank:
                if qtype == 3:
                    j = normalize_judgement(bank, self.state)
                    answer_list = [j] if j else None
                elif qtype in (0, 1):
                    answer_list = normalize_choice(bank, options, qtype == 1)
                    if answer_list is None:
                        log('WARN', f'题库答案无法匹配选项, 尝试其他方式: {bank[:50]}')
                else:
                    c = normalize_completion(bank)
                    answer_list = [c] if c else None

            if answer_list is None and self.state.llm_enabled():
                llm = self.state.query_llm(clean_q, options, qtype, img_urls)
                if llm:
                    if qtype == 3:
                        j = normalize_judgement(llm, self.state)
                        answer_list = [j] if j else None
                    elif qtype in (0, 1):
                        answer_list = normalize_choice(llm, options, qtype == 1)
                    else:
                        c = normalize_completion(llm)
                        answer_list = [c] if c else None

        # 选择类答案统一剥除会被 exe 切开的字符(对缓存/AI/题库来源一视同仁),
        # 剥除不影响 exe 后续按子序列匹配选项原文取字母
        if answer_list and qtype in (0, 1):
            stripped = [a for a in (strip_cut_chars(x) for x in answer_list) if a]
            if stripped:
                answer_list = stripped

        if answer_list:
            log('INFO', f'✓ 最终答案: {short_q} -> {"#".join(answer_list)[:80]}')
        else:
            note = '题库无答案' + ('' if self.state.llm_enabled() else '(未配置大模型兜底' + (
                ', 且题目含图' if img_urls else '') + ')')
            log('WARN', f'✗ 未获得答案: {short_q}  [选项数:{len(options)}, {note}]')
        if not hit:
            self.state.cache_set(ckey, answer_list)
        self._send({'answer': {'bestAnswer': answer_list or []}})


def main():
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    state = ProxyState()
    Handler.state = state
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    log('INFO', f'言溪题库本地融合代理已启动: http://127.0.0.1:{PORT}/adapter')
    log('INFO', f'已加载 {len(state.tokens)} 个TOKEN, 判定词: 正确="{state.true_target}" 错误="{state.false_target}"')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log('INFO', '已停止')


if __name__ == '__main__':
    main()

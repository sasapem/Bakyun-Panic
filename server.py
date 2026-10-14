#!/usr/bin/env python3
"""
AIワニワニパニック用 サーバー
- 静的ファイル配信 (index.html など)
- /api/ai POST: Gemini APIプロキシ
- /api/generate-character POST: プレイログ → Gemini概念設計 → Flux画像生成 → 新キャラ
"""
import http.server, json, os, urllib.request, re, random, time, base64, threading

# -------------------- Gemini key --------------------
def load_gemini_key():
    k = os.environ.get('GEMINI_API_KEY')
    if k: return k
    try:
        with open(os.path.expanduser('~/.gemini_env')) as f:
            for line in f:
                if 'GEMINI_API_KEY' in line and '=' in line:
                    return line.split('=',1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None

GEMINI_KEY = load_gemini_key()
print(f'[r3] Gemini key: {"LOADED" if GEMINI_KEY else "MISSING"}')

POS_NAMES = ['左上','中上','右上','左下','中下','右下']

# -------------------- Prompt builders --------------------
def build_live_prompt(log):
    """生プレイ中の分析(重み+コメント)"""
    return (
        "あなたはワニワニパニック(ゲーセン風)のAIアシスタントです。\n"
        "6つの穴(index: 0=左上 1=中上 2=右上 3=左下 4=中下 5=右下)から\n"
        "敵が出てくる頻度を、プレイヤーの癖を踏まえて調整します。\n\n"
        f"直近のプレイログ(JSON):\n{json.dumps(log, ensure_ascii=False)}\n\n"
        "以下のJSONを出力してください(他の文字は絶対に含めない):\n"
        '{"weights":{"0":1.0,"1":1.0,"2":1.0,"3":1.0,"4":1.0,"5":1.0},'
        '"comment":"短い一言","summary":"一文"}\n\n'
        "ルール:\n"
        "- 各weightは 0.5 〜 1.5\n"
        "- よく叩かれている穴は少しweightを下げる(多様化)\n"
        "- ミスが多い・反応が遅い穴はやや上げる(弱点突く)\n"
        "- ただし差は最大でも1.5倍程度。理不尽にしない\n"
        "- commentは10〜18文字、日本語で煽り口調や短いヒント\n"
        "- summaryは30文字以内、日本語\n"
        "- 絶対にJSON以外の文字(コードブロック含め)は含めない"
    )

def build_final_prompt(log, score, max_combo):
    """ゲーム終了後の一言分析"""
    return (
        "ワニワニパニックを終えたプレイヤーの総合データからAI分析コメントを書いてください。\n\n"
        f"スコア:{score} 最大コンボ:{max_combo}\n"
        f"プレイログ:\n{json.dumps(log, ensure_ascii=False)}\n\n"
        '次のJSONだけを出力: {"weights":{"0":1,"1":1,"2":1,"3":1,"4":1,"5":1},'
        '"comment":"短い一言","summary":"40文字以内の総合分析"}\n'
        "- summaryは「あなたは◯◯タイプ」など、プレイヤーの個性を具体的に指摘\n"
        "- 褒め言葉と軽い指摘を混ぜて\n"
        "- 日本語、絶対にJSON以外の文字(コードブロック含む)を出力しない"
    )

# -------------------- Gemini call --------------------
def gemini_call(prompt, timeout=6):
    if not GEMINI_KEY:
        return None
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}'
    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.8,
            'maxOutputTokens': 250,
            'responseMimeType': 'application/json',
        }
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        text = resp.get('candidates',[{}])[0].get('content',{}).get('parts',[{}])[0].get('text','').strip()
        # Be lenient: strip ```json ... ``` if present
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            text = m.group()
        data = json.loads(text)
        return data
    except Exception as e:
        print(f'[r3] gemini err: {e!r}')
        return None

# -------------------- Character generation (Gemini + Flux) --------------------
def generate_character(log):
    """プレイログ → Geminiでコンセプト → Fluxで画像生成 → 新キャラ定義を返す"""
    # 1. Gemini: コンセプト案
    concept_prompt = (
        "ワニワニパニックゲームで、プレイヤーの行動ログに基づいて"
        "新しいピクセル絵の敵キャラクターを1体考えてください。\n"
        f"ログ: {json.dumps(log, ensure_ascii=False)}\n\n"
        '次のJSONだけを出力してください:\n'
        '{"name":"日本語の短いキャラ名(6文字以内)",'
        '"prompt":"Fluxに渡す英語のピクセルアート画像プロンプト",'
        '"color":"#xxxxxx形式の色コード",'
        '"points":整数(1〜8)}'
        "\n\n制約:\n"
        "- promptは必ず英語、'pixel art 8-bit sprite' 必須、simple, cute, centered, transparent background 含める\n"
        "- プレイヤーの強み/弱みを反映(左攻めが多い→右特化、反応早い→素早いキャラ等)\n"
        "- JSON以外の文字は絶対に出力しない(コードブロックも禁止)"
    )
    concept = gemini_call(concept_prompt, timeout=8)
    if not concept or 'prompt' not in concept:
        # フォールバック: ランダムなシンプルキャラ
        concept = {
            'name': '謎の敵',
            'prompt': 'pixel art 8-bit sprite, cute monster, simple, centered, white background, bright colors',
            'color': '#ff4081',
            'points': 3,
        }
    # 2. Flux: 画像生成
    flux_prompt = concept['prompt'] + ', pixel art 8-bit sprite, simple design, white background, centered, no text, icon'
    try:
        body = json.dumps({
            'prompt': flux_prompt,
            'width': 256, 'height': 256,
            'num_inference_steps': 4,
            'guidance_scale': 0.0,
        }).encode()
        req = urllib.request.Request('http://localhost:8888/generate',
            data=body, headers={'Content-Type':'application/json'})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=20) as r:
            flux_resp = json.loads(r.read())
        elapsed = round(time.time()-t0, 2)
        print(f'[flux] generated in {elapsed}s: {concept.get("name")!r}')
        image_b64 = flux_resp.get('base64')
        if not image_b64:
            return None
    except Exception as e:
        print(f'[flux] err: {e}')
        return None
    return {
        'name':  str(concept.get('name', '謎の敵'))[:16],
        'points': int(concept.get('points', 3)) if isinstance(concept.get('points'), (int,float,str)) else 3,
        'color': str(concept.get('color', '#ff4081'))[:8],
        'imageDataUrl': 'data:image/png;base64,' + image_b64,
    }

# -------------------- Local fallback --------------------
def fallback_advise(log, mode='live', score=0, max_combo=0):
    weights = {}
    for i in range(6):
        h = log.get('hits', {}).get(str(i), log.get('hits', {}).get(i, 0))
        m = log.get('misses', {}).get(str(i), log.get('misses', {}).get(i, 0))
        r = log.get('avgReactionMs', {}).get(str(i), log.get('avgReactionMs', {}).get(i, 600))
        total = h + m
        w = 1.0
        if total >= 2:
            hit_rate = h / total if total > 0 else 0.5
            w *= 0.8 + (1 - hit_rate) * 0.5
            w *= 0.9 + min(0.4, max(0, (r - 500) / 1000))
        weights[str(i)] = round(max(0.5, min(1.5, w)), 2)
    # most targeted
    recent = log.get('recentTargets', []) or []
    counts = [0]*6
    for t in recent:
        try: counts[int(t)] += 1
        except: pass
    top_idx = counts.index(max(counts)) if counts else 0
    live_comments = [
        f'{POS_NAMES[top_idx]}ばかり',
        '読めてきた', '少し裏突く',
        f'{POS_NAMES[top_idx]}好きだね',
        '同じ所やめて',
    ]
    if mode == 'final':
        sm = build_final_summary(score, max_combo, log)
        return {'weights': weights, 'comment': '', 'summary': sm}
    return {
        'weights': weights,
        'comment': random.choice(live_comments),
        'summary': '',
    }

# -------------------- 多軸プレイスタイル診断 --------------------
def build_final_summary(score, max_combo, log):
    """score/max_combo/logから、多面的な「型」を組み合わせる"""
    hits = log.get('hits') or {}
    misses = log.get('misses') or {}
    react = log.get('avgReactionMs') or {}
    # 合計
    total_hits   = sum(int(hits.get(str(i), hits.get(i, 0)) or 0) for i in range(6))
    total_misses = sum(int(misses.get(str(i), misses.get(i, 0)) or 0) for i in range(6))
    miss_rate = total_misses / max(1, total_hits + total_misses)
    # 反応時間平均(ヒット重み付け)
    react_sum, react_n = 0.0, 0
    for i in range(6):
        h = int(hits.get(str(i), hits.get(i, 0)) or 0)
        r = int(react.get(str(i), react.get(i, 0)) or 0)
        if h > 0 and r > 0:
            react_sum += r * h; react_n += h
    avg_react = react_sum / react_n if react_n > 0 else 0
    # 左右・上下の偏り
    left  = sum(int(hits.get(str(i), hits.get(i, 0)) or 0) for i in [0,3])
    mid   = sum(int(hits.get(str(i), hits.get(i, 0)) or 0) for i in [1,4])
    right = sum(int(hits.get(str(i), hits.get(i, 0)) or 0) for i in [2,5])
    upper = sum(int(hits.get(str(i), hits.get(i, 0)) or 0) for i in [0,1,2])
    lower = sum(int(hits.get(str(i), hits.get(i, 0)) or 0) for i in [3,4,5])
    # カバー率(何穴から叩けたか)
    covered = sum(1 for i in range(6) if int(hits.get(str(i), hits.get(i, 0)) or 0) > 0)

    tags = []
    # スコア軸
    if score >= 40:      tags.append('神業級')
    elif score >= 25:    tags.append('ベテラン')
    elif score >= 15:    tags.append('なかなか')
    elif score >= 8:     tags.append('ほどほど')
    else:                tags.append('おてやわらか')
    # コンボ軸
    if max_combo >= 12:  tags.append('コンボの鬼')
    elif max_combo >= 7: tags.append('リズミカル')
    elif max_combo >= 4: tags.append('流れ型')
    # 反応時間軸
    if react_n >= 3:
        if avg_react < 450:  tags.append('電光石火')
        elif avg_react < 650: tags.append('素早い')
        elif avg_react > 900: tags.append('じっくり派')
    # 偏り軸
    if total_hits >= 6:
        if left > right*1.8:   tags.append('左ひいき')
        elif right > left*1.8: tags.append('右ひいき')
        elif mid > (left+right)*0.5: tags.append('ど真ん中派')
        if upper > lower*1.6: tags.append('上段職人')
        elif lower > upper*1.6: tags.append('地面派')
    # カバー軸
    if covered == 6 and total_hits >= 6:   tags.append('万能カバー')
    elif covered <= 2 and total_hits >= 4: tags.append('一点集中')
    # ミス率軸
    if total_hits + total_misses >= 6:
        if miss_rate < 0.12:  tags.append('ミス少なめ')
        elif miss_rate > 0.45: tags.append('豪快派')
    # フレーバー(ランダム味付け)
    flavors = [
        '熟練の手つき', 'AIが見切れない', '読みの達人', 'テンポ良し',
        '静かな殺し屋', '緊張に強い', '情熱型', '独特のリズム', '渋い魅せ方',
    ]
    if random.random() < 0.5:
        tags.append(random.choice(flavors))
    # 2〜3個組み合わせる
    random.shuffle(tags)
    picked = tags[:3] if len(tags) >= 3 else tags[:2]
    if not picked:
        picked = ['挑戦者']
    joined = '・'.join(picked)
    endings = [
        f'{joined} タイプ',
        f'{joined} の{score}点',
        f'あなたは {joined}',
        f'今日の顔:{joined}',
        f'{joined} でした',
    ]
    return random.choice(endings)

# -------------------- Main game shared status (妨害者ミラー用) --------------------
GAME_STATUS = {'score': 0, 'remainS': 0, 'phase': 'waiting', 'isFever': False,
               'lastChar': None, 'updatedAt': 0}

# -------------------- Sabotage events (妨害者用キュー) --------------------
SABOTAGE = {'events': [], 'seq': 0}

# --- 妨害者マネジメント(順番制) ---
# 登録された妨害者のうち一人をランダムに選んで「妨害チャンス」を渡す
SABO_MGR = {
    'saboteurs': {},     # id -> {'lastSeen': ms}
    'turn': None,        # {'id': str, 'validUntil': ms}
    'nextTurnAt': 0,     # ms
    'seqId': 0,
}
TURN_INTERVAL_MS = 5000    # 前回ターン開始からの最短間隔
TURN_WINDOW_MS   = 3500    # ターン有効時間(この間にタップ)
SABOTEUR_TIMEOUT_MS = 12000

def register_saboteur(name=''):
    SABO_MGR['seqId'] += 1
    sid = f'S{SABO_MGR["seqId"]}'
    SABO_MGR['saboteurs'][sid] = {
        'lastSeen': int(time.time()*1000),
        'name': (name or f'妨害者{SABO_MGR["seqId"]}')[:16],
        'fires': {'bomb': 0, 'wave': 0, 'fast': 0, 'total': 0},
        'joinedAt': int(time.time()*1000),
    }
    return sid

def reset_saboteur_stats():
    for sid in SABO_MGR['saboteurs']:
        SABO_MGR['saboteurs'][sid]['fires'] = {'bomb': 0, 'wave': 0, 'fast': 0, 'paint': 0, 'total': 0}
        SABO_MGR['saboteurs'][sid]['lastFire'] = 0
    # イベントキューもクリア(前回ゲームの残り妨害が新ゲームに入るのを防ぐ)
    SABOTAGE['events'] = []

def touch_saboteur(sid):
    if sid in SABO_MGR['saboteurs']:
        SABO_MGR['saboteurs'][sid]['lastSeen'] = int(time.time()*1000)

def is_my_turn(sid):
    t = SABO_MGR['turn']
    if not t: return False
    if t['id'] != sid: return False
    if int(time.time()*1000) > t['validUntil']: return False
    return True

def turn_manager_thread():
    import random as _r
    while True:
        time.sleep(0.2)
        now = int(time.time()*1000)
        # 死んだ妨害者を掃除
        for sid in list(SABO_MGR['saboteurs']):
            if now - SABO_MGR['saboteurs'][sid]['lastSeen'] > SABOTEUR_TIMEOUT_MS:
                del SABO_MGR['saboteurs'][sid]
                if SABO_MGR['turn'] and SABO_MGR['turn']['id'] == sid:
                    SABO_MGR['turn'] = None
        # 現在のターンが期限切れならクリア
        if SABO_MGR['turn'] and now > SABO_MGR['turn']['validUntil']:
            SABO_MGR['turn'] = None
        # 新しいターンを選ぶ
        if SABO_MGR['turn'] is None and now >= SABO_MGR['nextTurnAt']:
            if SABO_MGR['saboteurs']:
                chosen = _r.choice(list(SABO_MGR['saboteurs'].keys()))
                SABO_MGR['turn'] = {
                    'id': chosen,
                    'validUntil': now + TURN_WINDOW_MS,
                }
                SABO_MGR['nextTurnAt'] = now + TURN_INTERVAL_MS
                print(f'[turn] → {chosen} (active: {len(SABO_MGR["saboteurs"])})')

threading.Thread(target=turn_manager_thread, daemon=True).start()

def queue_sabotage(action, hole=None, saboteur_name=''):
    SABOTAGE['seq'] += 1
    SABOTAGE['events'].append({
        'seq': SABOTAGE['seq'],
        'action': action,
        'hole': hole if hole is not None else random.randint(0,5),
        'name': saboteur_name,
        't': int(time.time()*1000),
    })
    SABOTAGE['events'] = SABOTAGE['events'][-200:]
    return SABOTAGE['seq']

# -------------------- HTTP handler --------------------
class H(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        super().end_headers()
    def do_OPTIONS(self):
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass

    def _json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith('/api/sabotage-events'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try: since = int(q.get('since', ['0'])[0])
            except: since = 0
            events = [e for e in SABOTAGE['events'] if e['seq'] > since]
            self._json({'events': events, 'lastSeq': SABOTAGE['seq']})
            return
        if self.path == '/api/game-status':
            self._json(GAME_STATUS); return
        if self.path == '/api/sabotage-stats':
            stats = []
            for sid, r in SABO_MGR['saboteurs'].items():
                f = r.get('fires', {})
                if f.get('total', 0) > 0:
                    stats.append({
                        'id': sid,
                        'name': r.get('name', sid),
                        'paint': f.get('paint', 0),
                        'total': f.get('total', 0),
                    })
            stats.sort(key=lambda x: -x['total'])
            self._json({'stats': stats})
            return
        if self.path.startswith('/api/my-turn'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            sid = q.get('id', [''])[0]
            touch_saboteur(sid)
            now = int(time.time()*1000)
            turn = SABO_MGR['turn']
            active = len(SABO_MGR['saboteurs'])
            if turn and turn['id'] == sid:
                self._json({'isMyTurn': True, 'remainMs': max(0, turn['validUntil'] - now), 'activeCount': active})
            else:
                self._json({'isMyTurn': False, 'nextAt': SABO_MGR['nextTurnAt'], 'activeCount': active})
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == '/api/register':
            length = int(self.headers.get('Content-Length', 0))
            try: body = json.loads(self.rfile.read(length) or b'{}')
            except Exception: body = {}
            sid = register_saboteur(body.get('name', ''))
            print(f'[register] {sid}')
            self._json({'id': sid, 'name': SABO_MGR['saboteurs'][sid]['name']})
            return

        if self.path == '/api/sabotage-reset':
            reset_saboteur_stats()
            self._json({'ok': True})
            return

        if self.path == '/api/game-status':
            length = int(self.headers.get('Content-Length', 0))
            try: body = json.loads(self.rfile.read(length) or b'{}')
            except Exception: body = {}
            for k in ('score','remainS','phase','isFever','moles'):
                if k in body: GAME_STATUS[k] = body[k]
            GAME_STATUS['updatedAt'] = int(time.time()*1000)
            self._json({'ok': True}); return

        if self.path == '/api/sabotage':
            length = int(self.headers.get('Content-Length', 0))
            try: body = json.loads(self.rfile.read(length) or b'{}')
            except Exception: body = {}
            sid = body.get('id', '')
            touch_saboteur(sid)
            # 個人クールダウンチェック(6秒)
            COOLDOWN_MS = 6000
            rec = SABO_MGR['saboteurs'].get(sid, {})
            now_t = int(time.time()*1000)
            last_fire = rec.get('lastFire', 0)
            if now_t - last_fire < COOLDOWN_MS:
                wait = COOLDOWN_MS - (now_t - last_fire)
                self._json({'ok': False, 'reason': 'cooldown', 'waitMs': wait})
                return
            action = 'paint'  # 唯一のアクション: ペイント(画面を隠す)
            sab_name = SABO_MGR['saboteurs'].get(sid, {}).get('name', '誰か')
            # 位置(正規化 0.0〜1.0)と色
            try:
                x = float(body.get('x', 0.5))
                y = float(body.get('y', 0.5))
            except: x, y = 0.5, 0.5
            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            color = body.get('color') or random.choice(['#ff1744','#00c853','#2979ff','#ffd600','#d500f9','#ff6d00'])
            SABOTAGE['seq'] += 1
            SABOTAGE['events'].append({
                'seq': SABOTAGE['seq'], 'action': action,
                'x': x, 'y': y, 'color': color,
                'name': sab_name, 't': int(time.time()*1000),
            })
            SABOTAGE['events'] = SABOTAGE['events'][-200:]
            seq = SABOTAGE['seq']
            if sid in SABO_MGR['saboteurs']:
                SABO_MGR['saboteurs'][sid]['lastFire'] = now_t
                f = SABO_MGR['saboteurs'][sid].get('fires', {'bomb':0,'wave':0,'fast':0,'total':0})
                f[action] = f.get(action, 0) + 1
                f['total'] = f.get('total', 0) + 1
                SABO_MGR['saboteurs'][sid]['fires'] = f
            print(f'[sabotage] {sid} action={action} seq={seq}')
            self._json({'ok': True, 'seq': seq, 'action': action})
            return

        if self.path == '/api/generate-character':
            length = int(self.headers.get('Content-Length', 0))
            try: body = json.loads(self.rfile.read(length) or b'{}')
            except Exception: body = {}
            log = body.get('log') or {}
            result = generate_character(log)
            if result is None:
                self._json({'error':'generation failed'}, 503); return
            self._json(result); return

        if self.path == '/api/ai':
            length = int(self.headers.get('Content-Length', 0))
            try: body = json.loads(self.rfile.read(length) or b'{}')
            except Exception: body = {}
            log = body.get('log') or {}
            mode = body.get('mode', 'live')
            score = int(body.get('score', 0))
            max_combo = int(body.get('maxCombo', 0))
            # 1. Gemini先行
            prompt = build_final_prompt(log, score, max_combo) if mode == 'final' else build_live_prompt(log)
            t0 = time.time()
            data = gemini_call(prompt, timeout=6)
            elapsed = round(time.time()-t0, 2)
            # 2. 失敗時フォールバック
            if data is None or 'weights' not in data:
                data = fallback_advise(log, mode=mode, score=score, max_combo=max_combo)
                data['_source'] = 'fallback'
                print(f'[ai] fallback used (elapsed={elapsed}s)')
            else:
                data['_source'] = 'gemini'
                print(f'[ai] gemini ok (elapsed={elapsed}s) comment={data.get("comment","")!r}')
            self._json(data)
            return
        self.send_error(404)

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    port = 8767
    print(f'[r3] static + /api/ai on 0.0.0.0:{port}')
    http.server.ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()

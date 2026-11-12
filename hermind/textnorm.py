"""Text normalisation, tokenisation, date parsing and kind classification for hermind. Pure stdlib.

* normalize(): NFKC (full-width -> half-width), lowercase, simplified -> traditional Chinese for common characters
  (so 用户/用戶, 剧本/劇本 index identically). Applied to notes, keywords, queries and synonyms alike.
* tokenize(): CJK runs -> character unigrams + bigrams; Latin words -> light English stem (closing/closed -> clos),
  plus the parts of compound tokens (llama.cpp-server -> llama, cpp, server).
* parse_dates(): ISO / 年月日 / 月日 / English month dates found in a text.
* classify_kind(): decision / preference / tech / news / chat / state from cue words and structure.
"""

from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------------------------------------------
# Simplified -> traditional for common characters (pairs "簡繁"). Only used for matching; stored text is untouched.
# Characters whose simplified form maps to several traditional ones in common use (干 台 只 面 系 松 脏 …) are left out.
_S2T_PAIRS = """
这這 个個 们們 来來 时時 会會 说說 对對 发發 过過 还還 没沒 着著 动動 后後 里裡 问問 间間 学學 关關 开開 现現
实實 点點 进進 种種 样樣 经經 长長 东東 车車 头頭 国國 话話 机機 电電 业業 应應 当當 体體 见見 为為 无無 与與
从從 两兩 产產 气氣 门門 场場 处處 务務 员員 数數 报報 题題 写寫 达達 设設 认認 识識 让讓 记記 论論 讲講 读讀
请請 调調 试試 语語 误誤 证證 词詞 该該 详詳 谁誰 谈談 课課 诉訴 译譯 议議 订訂 计計 训訓 许許 评評 访訪 询詢
购購 质質 费費 资資 贵貴 赛賽 账賬 负負 败敗 货貨 责責 贴貼 贷貸 贸貿 赞贊 输輸 转轉 软軟 轮輪 较較 辑輯 边邊
运運 选選 递遞 远遠 连連 适適 遗遺 钟鐘 钱錢 铁鐵 银銀 错錯 键鍵 锁鎖 链鏈 镜鏡 锋鋒 针針 钢鋼 销銷 锅鍋 闭閉
闲閒 闻聞 阅閱 队隊 阶階 际際 陆陸 陈陳 险險 随隨 隐隱 难難 页頁 顶頂 项項 顺順 须須 顾顧 预預 领領 频頻 颜顏
额額 风風 飞飛 饭飯 饮飲 馆館 马馬 驱驅 验驗 鱼魚 鸟鳥 龙龍 齐齊 么麼 广廣 庆慶 库庫 废廢 异異 弃棄 张張 弹彈
强強 归歸 录錄 彻徹 忆憶 态態 总總 恶惡 悬懸 惊驚 惯慣 戏戲 战戰 户戶 扩擴 扫掃 护護 担擔 拥擁 择擇 挂掛 换換
据據 损損 摄攝 摆擺 携攜 敌敵 断斷 显顯 术術 杀殺 杂雜 权權 条條 杨楊 极極 构構 标標 档檔 桥橋 检檢 楼樓 横橫
欢歡 历歷 毕畢 汇匯 沟溝 泽澤 洁潔 测測 浏瀏 济濟 浅淺 涨漲 渐漸 满滿 滚滾 灭滅 灵靈 炉爐 炼煉 烦煩 热熱 爱愛
状狀 独獨 环環 画畫 畅暢 疗療 盘盤 监監 确確 码碼 础礎 离離 积積 称稱 稳穩 穷窮 竞競 笔筆 签簽 简簡 类類 纪紀
约約 级級 纯純 纳納 纸紙 线線 练練 组組 细細 终終 结結 给給 络絡 绝絕 统統 继繼 续續 维維 综綜 绿綠 编編 缓緩
缩縮 网網 罗羅 联聯 肤膚 脑腦 脚腳 节節 获獲 范範 荐薦 药藥 萨薩 华華 莱萊 虑慮 虽雖 补補 装裝 规規 视視 览覽
觉覺 观觀 触觸 讯訊 诊診 诚誠 谢謝 贝貝 轨軌 轻輕 载載 辅輔 辆輛 办辦 劳勞 势勢 区區 医醫 单單 卖賣 卫衛 压壓
厂廠 厅廳 县縣 参參 双雙 变變 叙敘 号號 叶葉 吗嗎 启啟 响響 团團 园園 围圍 图圖 圆圓 圣聖 坏壞 块塊 坚堅 坛壇
声聲 备備 复復 够夠 夹夾 夺奪 奋奮 妇婦 妈媽 孙孫 宁寧 宝寶 审審 宪憲 宽寬 寻尋 导導 层層 届屆 属屬 岁歲 岛島
帅帥 师師 帐帳 带帶 帮幫 并並 庄莊 弯彎 径徑 优優 传傳 伤傷 伪偽 余餘 佣傭 侠俠 侣侶 侦偵 债債 倾傾 偿償 储儲
儿兒 党黨 兰蘭 兴興 养養 兽獸 内內 册冊 军軍 农農 冲衝 决決 况況 冻凍 净淨 准準 凉涼 减減 几幾 凤鳳 凭憑 击擊
刘劉 则則 刚剛 创創 删刪 别別 剂劑 剧劇 劝勸 励勵 劲勁 勋勳 匀勻 协協 卢盧 卧臥 却卻 厉厲 厌厭 叹嘆 吓嚇 听聽
吴吳 呜嗚 哑啞 唤喚 啸嘯 喷噴 嘱囑 垄壟 垒壘 尽盡 币幣 帘簾 庐廬 庙廟 忧憂 怀懷 怜憐 恋戀 恳懇 恼惱 悦悅 惩懲
愿願 执執 扬揚 扰擾 抚撫 抛拋 抢搶 拟擬 拦攔 挡擋 挤擠 挥揮 捞撈 掷擲 揽攬 摇搖 摊攤 撑撐 敛斂 斋齋 旧舊 旷曠
昼晝 晒曬 暂暫 朴樸 杰傑 枪槍 柜櫃 栏欄 树樹 梦夢 欧歐 残殘 毁毀 汉漢 汤湯 沪滬 泪淚 泼潑 浓濃 涂塗 润潤 渊淵
渔漁 温溫 湾灣 湿濕 溃潰 滞滯 滤濾 滥濫 灯燈 灾災 烂爛 烟煙 烧燒 焕煥 爷爺 牵牽 犹猶 狱獄 猎獵 猫貓 献獻 玛瑪
琐瑣 亩畝 痒癢 皱皺 盏盞 盐鹽 盖蓋 矫矯 矿礦 砖磚 硕碩 碍礙 礼禮 祸禍 秃禿 税稅 窃竊 竖豎 笼籠 筑築 筹籌 粮糧
紧緊 纠糾 红紅 纤纖 纬緯 纲綱 纵縱 纷紛 纹紋 纺紡 织織 绍紹 绑綁 绕繞 绘繪 绩績 绪緒 绵綿 缅緬 缘緣 缝縫 缴繳
罚罰 罢罷 职職 聪聰 肃肅 肠腸 肿腫 胁脅 胜勝 脉脈 脱脫 腾騰 舰艦 舱艙 艺藝 芦蘆 苏蘇 苹蘋 荣榮 莲蓮 营營 萝蘿
蓝藍 虚虛 虫蟲 虾蝦 蚁蟻 蜡蠟 衬襯 袜襪 誉譽 讨討 讳諱 讼訟 诈詐 诗詩 诞誕 诸諸 诺諾 谅諒 谊誼 谋謀 谎謊 谓謂
谣謠 谨謹 谱譜 贞貞 贡貢 财財 贩販 贪貪 贯貫 贱賤 赋賦 赌賭 赏賞 赔賠 赖賴 赚賺 赠贈 赵趙 赶趕 趋趨 跃躍 践踐
踪蹤 轰轟 辈輩 辉輝 辞辭 辽遼 迁遷 迈邁 违違 迟遲 逊遜 逻邏 邓鄧 邮郵 邻鄰 郑鄭 酱醬 释釋 鉴鑑 钓釣 钞鈔 钥鑰
钮鈕 铃鈴 铅鉛 铜銅 铭銘 铺鋪 锐銳 锡錫 锦錦 锻鍛 镇鎮 闪閃 闯闖 闷悶 闸閘 闹鬧 阀閥 阁閣 阔闊 阳陽 阴陰 阵陣
雾霧 静靜 韩韓 顽頑 顿頓 颗顆 飘飄 饥饑 饰飾 饱飽 饼餅 驶駛 驾駕 驻駐 骂罵 骗騙 骤驟 鲁魯 鲜鮮 鸡雞 鸭鴨 麦麥
黄黃 齿齒 龄齡 龟龜 丢丟 严嚴 丰豐 临臨 丽麗 举舉 乐樂 乔喬 习習 乡鄉 书書 买買 乱亂 争爭 亏虧 亚亞 亲親 亿億
仅僅 仑侖 仓倉 仪儀 价價 众眾 伞傘 伟偉 伦倫 侧側 侨僑 俭儉 俩倆 云雲 盘盤 览覽 档檔 储儲 启啟 爲為 裏裡 衞衛
啓啟 綫線 眞真 着著 钉釘 镑鎊 锈鏽 汇匯 剥剝 痴癡 晓曉 庞龐 宫宮 讶訝 诀訣 诫誡 诬誣 谜謎 谦謙 豪豪 贮貯 赁賃
贿賄 跷蹺 轴軸 轿轎 辖轄 辩辯 迹跡 铸鑄 锤錘 镶鑲 闽閩 阐闡 陕陝 隶隸 雏雛 鸣鳴 齐齊 坝壩 垦墾 埚堝 壳殼
"""
_S2T = {}
for _pair in _S2T_PAIRS.split():
    if len(_pair) == 2 and _pair[0] != _pair[1]:
        _S2T.setdefault(ord(_pair[0]), _pair[1])
del _pair

_CJK = "㐀-䶿一-鿿豈-﫿"
RE_CJK_CHAR = re.compile(rf"[{_CJK}]")
_RE_TOKEN = re.compile(rf"[{_CJK}]+|[0-9a-z][0-9a-z_.+\-]*[0-9a-z]|[0-9a-z]")
_RE_SPLIT = re.compile(r"[_.+\-]+")


# Invisible format characters (zero-width space/joiners, direction marks, word joiner, BOM, soft hyphen) are common in
# text pasted from web pages and chat apps; left in, they split one word into two tokens.
_INVISIBLE = {c: None for c in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x00AD)}


def normalize(text: str) -> str:
    """NFKC + lowercase + simplified->traditional (common characters), invisible format characters removed."""
    return unicodedata.normalize("NFKC", text or "").translate(_INVISIBLE).lower().translate(_S2T)


# Function characters / particles (Cantonese + written Chinese) that carry no topic on their own. They are dropped
# as unigrams, and a bigram made only of them is dropped as well.
STOP_CHARS = set(normalize(
    "我你佢他她它們们嘅咗係咩點嗰個个啲呢嗎吗呀啊嘛喎囉啦咋㗎吖的了是在有和與与及要會会唔冇都就也嚟去過过"
    "之其這这那哪邊边乜啱同將将把被給畀俾得着著而或又還还仲先再已曾最很好多少些樣样怎麼么甚什如果"
    "一上下中為为以對对從从到於于可能夠够想話话講讲問问答幫帮知道睇見见做用"))
STOP_LATIN = {"the", "a", "an", "is", "are", "of", "to", "and", "or", "in", "on", "for", "what", "how", "why",
              "do", "does", "did", "i", "my", "we", "our", "it", "be", "with", "that", "this", "was", "were", "as",
              "at", "by", "from", "has", "have", "had", "its", "not", "but", "if", "so", "than", "then", "there",
              "which", "who", "when", "where", "will", "would", "can", "could", "should", "into", "about", "also",
              "involving"}
UNIGRAM_WEIGHT, BIGRAM_WEIGHT, LATIN_WEIGHT = 0.35, 1.0, 1.2


def stem(word: str) -> str:
    """Very light English suffix stripping (plural / -ing / -ed / trailing e), applied to alphabetic words only."""
    w = word
    if not w.isalpha() or len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    elif w.endswith("es") and len(w) > 4 and w[-3] in "sxz":
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        w = w[:-1]
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    # derivational suffixes (presentation/present, detection/detect, measurement/measure). Both the indexed text and
    # the query go through the same function, so over-stripping is harmless as long as it is consistent; keep a
    # stem of >= 4 letters so short words are not mangled.
    for suf in ("ations", "ation", "ments", "ment", "ions", "ion", "ness", "ities", "ity"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[:-len(suf)]
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def tokenize(text: str, normalized: bool = False) -> list[str]:
    """Latin words (normalised + stemmed, plus compound parts) and, for every CJK run, unigrams and bigrams."""
    t = text if normalized else normalize(text)
    out: list[str] = []
    for m in _RE_TOKEN.finditer(t):
        tok = m.group(0)
        if RE_CJK_CHAR.match(tok):
            out.extend(tok)
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
            continue
        if tok in STOP_LATIN:
            continue
        out.append(stem(tok))
        if _RE_SPLIT.search(tok):
            parts = [p for p in _RE_SPLIT.split(tok) if p]
            if len(parts) > 1:
                out.extend(stem(p) for p in parts if (len(p) > 1 or p.isdigit()) and p not in STOP_LATIN)
    return out


def token_weight(tok: str) -> float:
    if RE_CJK_CHAR.match(tok):
        if len(tok) == 1:
            return 0.0 if tok in STOP_CHARS else UNIGRAM_WEIGHT
        return 0.0 if all(c in STOP_CHARS for c in tok) else BIGRAM_WEIGHT
    return 0.0 if tok in STOP_LATIN else LATIN_WEIGHT


# Query-side only (the index is untouched). Conversational filler carries no topic: "幫我…一下…謝謝" pulled any note
# with the same politeness words. Generic task words ("排查 問題 測試") match every debugging note, so they keep only a
# fraction of their weight and the topic word ("memory", "Atlas") decides the ranking.
QUERY_FILLER = tuple(sorted({normalize(x) for x in (
    "謝謝 多謝 唔該 麻煩 請問 你覺得 我覺得 覺得 幫我 幫手 幫忙 一下 然後 跟住 的話 怎麼樣 怎麼 點樣 點解 可唔可以 "
    "可不可以 可以 能不能 能否 是不是 係唔係 有沒有 有冇 其實 應該 或者 還有 所以 因為 但是 不過 那麼 這樣 咁樣 "
    "這個 那個 呢個 嗰個 什麼 甚麼 咩嘢 哪裡 哪裏 邊度 好嗎 好唔好 一些 一點 少少 重新 再次 繼續 告訴我 話我知 "
    "也就是說 即係 對吧 對嗎 是吧 係咪 給我原因和理由 原因和理由 原因同理由 啊 呀 吧 囉 喇 啦").split()}, key=len, reverse=True))
GENERIC_QUERY_TERMS = tuple(sorted({normalize(x) for x in (
    "問題 排查 測試 出錯 錯誤 修復 修正 優化 東西 情況 方法 處理 檢查 解決 幫助 改善 進步 嘗試 看看 睇睇 推薦 原因 理由").split()},
    key=len, reverse=True))
GENERIC_FACTOR = 0.4
STOP_BIGRAM_FACTOR = 0.5


def query_parts(query: str) -> tuple[str, list[str]]:
    """(normalised query with filler and generic words blanked out, the generic words found)."""
    stripped = normalize(query)
    for w in QUERY_FILLER:
        stripped = stripped.replace(w, " ")
    generic: list[str] = []
    for w in GENERIC_QUERY_TERMS:
        if w in stripped:
            generic.append(w)
            stripped = stripped.replace(w, " ")
    return stripped, generic


def query_term_factor(term: str, query: str) -> float:
    """Weight factor for a synonym-table term found in *query*: 0 when it only occurs inside filler, GENERIC_FACTOR
    for a generic word, else 1. Without it the expansions of 東西 / 原因 / 推薦 re-added the weight the filler and
    generic handling had removed."""
    t = normalize(term)
    stripped, generic = query_parts(query)
    if t in stripped:
        return 1.0
    if any(t in g or g in t for g in generic):
        return GENERIC_FACTOR
    return 0.0


def query_token_weights(query: str, fallback: bool = True, stop_bigram_factor: float = 1.0) -> dict[str, float]:
    """token -> query weight with filler removed and generic task words down-weighted. Falls back to the plain
    token_weight() of the whole query when nothing topical is left (a query made only of filler / generic words)."""
    t = normalize(query)
    plain: dict[str, float] = {}
    for tok in tokenize(t, normalized=True):
        plain[tok] = max(plain.get(tok, 0.0), token_weight(tok))
    stripped, generic = query_parts(query)
    out: dict[str, float] = {}
    for tok in tokenize(stripped, normalized=True):
        w = token_weight(tok)
        if stop_bigram_factor != 1.0 and w and len(tok) == 2 and RE_CJK_CHAR.match(tok) \
                and any(c in STOP_CHARS for c in tok):
            w *= stop_bigram_factor   # "修就": one real character glued to a particle (gate only: "偏好", "電話" too)
        out[tok] = max(out.get(tok, 0.0), w)
    if not any(v > 0 for v in out.values()):
        return plain if fallback else {}
    for g in generic:
        for tok in tokenize(g, normalized=True):
            w = token_weight(tok) * GENERIC_FACTOR
            if w > out.get(tok, 0.0):
                out[tok] = w
    return out


def topical_weight(query: str) -> float:
    """Sum of query weights left after query_token_weights(): near zero for a prompt with no topic of its own
    ("你覺得要怎麼修就怎麼修謝謝"), where automatic recall can only return noise."""
    return sum(query_token_weights(query, fallback=False, stop_bigram_factor=STOP_BIGRAM_FACTOR).values())


def core_text(text: str) -> str:
    """Text without import metadata segments ("| When: …", "| Involving: …"), used for similarity."""
    parts = [p for p in re.split(r"\s*\|\s*", text or "")
             if not re.match(r"(?i)^(when|involving)\s*[:：]", p.strip())]
    return " | ".join(parts) if parts else (text or "")


def sim_features(text: str) -> frozenset:
    """Feature set for near-duplicate detection: CJK character bigrams + Latin words (normalised)."""
    t = normalize(core_text(text))
    feats = set()
    for m in _RE_TOKEN.finditer(t):
        tok = m.group(0)
        if RE_CJK_CHAR.match(tok):
            if len(tok) == 1:
                feats.add(tok)
            feats.update(tok[i:i + 2] for i in range(len(tok) - 1))
        elif tok not in STOP_LATIN:
            feats.add(stem(tok))
    return frozenset(feats)


_RE_NUM = re.compile(r"(?<![0-9a-z.])(\d+(?:[.,]\d+)?)\s*(gb|mb|kb|tb|g|m|k|%|ms|s|秒|分鐘|小時|天|個|核|vcore|元|港元|美元|usd|hkd)?",
                     re.IGNORECASE)


def numbers(text: str) -> set[str]:
    """Quantities mentioned in a text (number + unit), ignoring dates and times, for conflict detection."""
    t = normalize(core_text(text))
    t = RE_DATE_ANY.sub(" ", t)
    t = re.sub(r"\d{1,2}:\d{2}", " ", t)
    return {(m.group(1) + (m.group(2) or "")).replace(",", "") for m in _RE_NUM.finditer(t)}


# ---------------------------------------------------------------------------------------------------------------
# Dates

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_RE_ISO = re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_RE_YMD_CJK = re.compile(r"(20\d{2}|19\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]")
_RE_MD_CJK = re.compile(r"(?<![\d年])(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]")
_RE_EN = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
                    r"(?:,?\s+(\d{4}))?\b", re.IGNORECASE)
_RE_EN2 = re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(\d{4}))?\b",
                     re.IGNORECASE)
RE_DATE_ANY = re.compile("|".join(p.pattern for p in (_RE_ISO, _RE_YMD_CJK, _RE_MD_CJK)))


def _ts(y: int, m: int, d: int) -> float | None:
    try:
        return datetime(y, m, d).timestamp()
    except ValueError:
        return None


def parse_dates(text: str, ref: float | None = None) -> list[float]:
    """Timestamps (local midnight) of explicit dates in *text*. Dates without a year take the year of *ref* (now)."""
    t = unicodedata.normalize("NFKC", text or "")
    year = time.localtime(ref if ref is not None else time.time()).tm_year
    found: list[tuple[int, float]] = []
    for rx in (_RE_ISO, _RE_YMD_CJK):
        for m in rx.finditer(t):
            ts = _ts(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if ts:
                found.append((m.start(), ts))
    taken = {p for p, _ in found}
    for m in _RE_MD_CJK.finditer(t):
        if m.start() not in taken:
            ts = _ts(year, int(m.group(1)), int(m.group(2)))
            if ts:
                found.append((m.start(), ts))
    for m in _RE_EN.finditer(t):
        ts = _ts(int(m.group(3) or year), _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        if ts:
            found.append((m.start(), ts))
    for m in _RE_EN2.finditer(t):
        ts = _ts(int(m.group(3) or year), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        if ts:
            found.append((m.start(), ts))
    found.sort()
    return list(dict.fromkeys(ts for _, ts in found))


PERIOD_RANGES = True  # v0.3 calendar periods / "N days ago"; False = v0.2 behaviour (for A/B)

_RELATIVE = [  # (pattern, days_back_start, days_back_end)  -> window [today-start, today-end+1)
    (re.compile(r"今日|今天|today"), 0, 0),
    (re.compile(r"琴日|尋日|噚日|昨天|昨日|yesterday"), 1, 1),
    (re.compile(r"大前日|大前天"), 3, 3),
    (re.compile(r"前日|前天"), 2, 2),
    (re.compile(r"呢個星期|呢個禮拜|這個星期|本週|本周|this week"), 6, 0),
    (re.compile(r"上個星期|上星期|上個禮拜|上週|上周|last week"), 13, 7),
    (re.compile(r"呢排|最近|近排|近來|recently|lately"), 13, 0),
]


# Whole-period expressions become calendar RANGES (Hindsight chinese_temporal_periods lesson); 個 is optional
# (上個月 / 上月), 星期 / 禮拜 / 週 / 周 / week are interchangeable. normalize() already maps 个/后/两/号/钟 to 個/後/兩/號/鐘.
_WEEK = r"(?:星期|禮拜|週|周|week)"
_PERIODS = [  # (pattern, unit, offset)  unit: week | month | year; offset 0 = current, -1 = previous
    (re.compile(rf"(?:呢|今|這|这|本)個?{_WEEK}|this week"), "week", 0),
    (re.compile(rf"上個?{_WEEK}|last week"), "week", -1),
    (re.compile(r"(?:呢|今|這|这|本)個?月|this month"), "month", 0),
    (re.compile(r"上個?月|last month"), "month", -1),
    (re.compile(r"今年|this year"), "year", 0),
    (re.compile(r"去年|舊年|last year"), "year", -1),
]
_NUM_CJK = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
            "半": 0.5, "幾": 3}
_RE_AGO = re.compile(r"(\d+|[一二兩三四五六七八九十半幾])\s*(?:個)?\s*(日|天|星期|禮拜|週|周|月)\s*(?:前|之前|以前)|"
                     r"(\d+)\s*(day|week|month)s?\s+ago")


def _period(today: datetime, unit: str, offset: int) -> tuple[float, float]:
    if unit == "week":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        return start.timestamp(), (start + timedelta(days=7)).timestamp()
    if unit == "month":
        y, m = today.year, today.month + offset
        while m < 1:
            y, m = y - 1, m + 12
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        return datetime(y, m, 1).timestamp(), datetime(ny, nm, 1).timestamp()
    return datetime(today.year + offset, 1, 1).timestamp(), datetime(today.year + offset + 1, 1, 1).timestamp()


def query_time_window(query: str, now: float) -> tuple[float, float] | None:
    """(start, end) implied by explicit or relative dates in a query, or None."""
    q = normalize(query)
    dates = parse_dates(query, ref=now)
    if dates:
        return min(dates), max(dates) + 86400
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if PERIOD_RANGES:
        m = _RE_AGO.search(q)
        if m:
            n = float(m.group(1)) if (m.group(1) or "").isdigit() else _NUM_CJK.get(m.group(1) or "", 0)
            if m.group(3):
                n = float(m.group(3))
            unit = m.group(2) or m.group(4)
            days = n * (30 if unit in ("月", "month") else 7 if unit in ("星期", "禮拜", "週", "周", "week") else 1)
            mid = today - timedelta(days=days)
            pad = max(1.0, days * 0.25)
            return (mid - timedelta(days=pad)).timestamp(), (mid + timedelta(days=pad + 1)).timestamp()
        for rx, unit, off in _PERIODS:
            if rx.search(q):
                start, end = _period(today, unit, off)
                return start, min(end, now + 1)  # the current period ends now
    for rx, start, end in _RELATIVE:
        if rx.search(q):
            return ((today - timedelta(days=start)).timestamp(), (today - timedelta(days=end - 1)).timestamp())
    if re.search(r"呢個月|這個月|本月|this month", q):
        return today.replace(day=1).timestamp(), now + 1
    return None


# ---------------------------------------------------------------------------------------------------------------
# Kind classification (imports, and memory_remember with kind="auto"). Latin letters alone never mean "tech".

KINDS = ("decision", "preference", "tech", "news", "chat", "state", "summary")  # summary: LLM-consolidated, built offline

_RX = lambda s: re.compile(s, re.IGNORECASE)  # noqa: E731
_CUES = {
    "decision": _RX(r"決定|決議|結論|定案|批准|拍板|同意(?:採用|使用|改用)|選定|選用|改用|採用|改為|不採用|放棄|否決|"
                    r"暫不|不建議|建議(?:使用|採用|改用|先)|方案是|最終|政策|共識|\bdecided\b|\bdecision\b|\bagreed\b|"
                    r"\bapproved\b|\bchose\b|\bchosen\b|\bopted\b|\bwill use\b|\bswitch(?:ed)? to\b|"
                    r"\brecommend(?:s|ed)?\b|\bpolicy\b|\bconclusion\b"),
    "preference": _RX(r"偏好|喜歡|鍾意|唔鍾意|不喜歡|討厭|習慣|希望|想要|不想|唔想|傾向|寧願|要求(?:你|助手|assistant)|"
                      r"風格|口吻|語氣|格式要求|一律|總是|永遠不要|\bprefers?\b|\bpreference\b|\blikes?\b|\bdislikes?\b|"
                      r"\bwants?\b|\bhates?\b|\balways\b|\bnever\b|\bstyle\b"),
    "news": _RX(r"報導|報道|據報|消息指|宣布|宣佈|發布|發佈|推出|上市|融資|收購|開源了|正式開放|全球開放|"
                r"\bannounc(?:e|ed|es)\b|\breleased?\b|\blaunch(?:ed|es)?\b|\bunveil(?:ed|s)?\b|\bbreaking\b|"
                r"\bheadline\b|\bacquir(?:e|ed|es)\b"),
    "state": _RX(r"目前|現時|現在|而家|剩餘|剩下|剩|仍然|仍|正在|狀態|運行中|在線|已滿|佔用|用量|使用率|餘額|額度|"
                 r"\bcurrently\b|\bnow\b|\bremaining\b|\bstatus\b|\bis running\b|\bfree\b|\bused\b|\busage\b|"
                 r"\bhas\s+\d|\bleft\b"),
    "tech": _RX(r"設定|配置|安裝|部署|錯誤|報錯|指令|命令|路徑|參數|腳本|日誌|編譯|程式碼|代碼|埠|端口|金鑰|密鑰|"
                r"環境變數|資料庫|伺服器|服務|進程|程序|版本|修補|修復|插件|外掛|API|SDK|CLI|\bconfig\w*|\binstall\w*|"
                r"\berror\b|\bcommand\b|\bscript\b|\bport\b|\bbuild\b|\bcompil\w+|\bdeploy\w*|\bdebug\w*|\bpatch\w*|"
                r"\bserver\b|\bprocess\b|\bdaemon\b|\bplugin\b|\bpackage\b|\bversion\b|\bbranch\b|\bcommit\b|"
                r"\blog\b|\bsshd?\b|\bdocker\b|\bproxy\b|\bendpoint\b|\btoken\b"),
}
_RE_CODEY = re.compile(r"`[^`]+`|(?:~|\.{0,2})/[\w.\-]+/|\b[\w\-]+\.(?:py|sh|json|ya?ml|toml|env|db|md|plist|txt|"
                       r"conf|log)\b|--[a-z][\w\-]+|\b\d{1,3}(?:\.\d{1,3}){3}\b|\b[a-z_]+=[\w\-./]+")
_RE_QUANTITY = re.compile(r"\d+(?:\.\d+)?\s*(?:GB|MB|TB|KB|G|M|%|vCore|核|個|元|美元|ms|秒)", re.IGNORECASE)
_RE_HAS_DATE = re.compile(r"When\s*:|20\d{2}[-年/]\d{1,2}|\d{1,2}月\d{1,2}日|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|"
                          r"oct|nov|dec)[a-z]*\.? \d{1,2}\b", re.IGNORECASE)


def kind_scores(text: str) -> dict[str, float]:
    t = unicodedata.normalize("NFKC", core_text(text or "")).translate(_S2T)
    s = {k: float(len(rx.findall(t))) for k, rx in _CUES.items()}
    s["tech"] += 1.5 * len(_RE_CODEY.findall(t))
    quantities = len(_RE_QUANTITY.findall(t))
    if quantities and (s["state"] or _RE_HAS_DATE.search(text or "")):
        s["state"] += 1.0 + 0.5 * quantities
    return s


def classify_kind(text: str, doc_hint: str = "") -> str:
    """decision / preference / news / state / tech / chat from cue words and structure (first match in that
    priority, with tech needing technical cues or code-like structure — Latin letters alone are not enough)."""
    s = kind_scores(text)
    if s["news"] and s["news"] >= s["decision"] and (s["news"] >= 1 or "news" in doc_hint.lower()):
        if s["news"] >= max(s["tech"], s["state"]) / 2:
            return "news"
    if s["decision"]:
        return "decision"
    if s["preference"] and s["preference"] >= s["tech"] / 2:
        return "preference"
    if s["state"] >= 2 or (s["state"] and s["state"] >= s["tech"]):
        return "state"
    if s["tech"] >= 1:
        return "tech"
    if "news" in doc_hint.lower():
        return "news"
    return "chat"


# ---------------------------------------------------------------------------------------------------------------
# Attribute slots ("swap 8GB", "RAM 3.7GB"); disk is left out on purpose: its figures mix total, free and cache for stale-state detection

_SLOT_ATTRS = {
    "swap": ("swap", "交換空間"),
    "ram": ("ram", "記憶體", "内存", "內存"),
}
_SLOT_ALT = "|".join(re.escape(w) for ws in _SLOT_ATTRS.values() for w in ws)
_QTY = r"(\d+(?:\.\d+)?)\s*(tb|tib|gb|gib|gi|g|mb|mib|mi|m)\b"
_RE_ATTR = re.compile(r"(%s)(?![a-z])" % _SLOT_ALT, re.I)
_RE_QTY = re.compile(_QTY, re.I)
_USAGE_WORDS = ("用", "remaining", "left", "目標", "目标", "預估", "预估", "常駐", "常驻", "<", ">", "≤", "預算", "已用", "使用", "用了", "用咗", "剩", "可用", "available", "used", "free", "usage", "%", "total used")
_UNIT_MB = {"tb": 1048576, "tib": 1048576, "gb": 1024, "gib": 1024, "gi": 1024, "g": 1024,
            "mb": 1, "mib": 1, "mi": 1, "m": 1}


_RE_CHANGE = re.compile(r"\s*(?:→|->|=>|to|變成|变成|增至|至)\s*" + _QTY, re.I)
_RE_LEAD = re.compile(_QTY + r"\s{0,3}(?:of\s+)?$", re.I)


def slot_values(text: str) -> dict[str, int]:
    """{attribute: size in MB} for capacity-like facts (swap/ram), deliberately conservative: a size counts
    only when it directly follows the attribute ("swap 8GB", "swap expanded from 4GB to 8GB" -> 8GB) or directly
    precedes it ("2GB swap"). Usage figures (used/available/%) and named drives ("Disk D") are ignored; when a
    note gives several totals the largest wins."""
    out: dict[str, int] = {}
    t = core_text(text).lower()
    for m in _RE_ATTR.finditer(t):
        if re.match(r"\s+[a-z](?![a-z])", t[m.end():m.end() + 3]):  # "Disk D" = a named drive, not a capacity
            continue
        word = m.group(1).lower()
        attr = next(a for a, ws in _SLOT_ATTRS.items() if word in ws)
        got = None
        nxt = _RE_ATTR.search(t, m.end())
        end = min(m.end() + 40, nxt.start() if nxt else len(t))
        after = re.split(r"[。;；\n]|\.\s", t[m.end():end])[0]
        best = 0
        for q in _RE_QTY.finditer(after):
            gap = after[:q.start()]
            tail = after[q.end():q.end() + 14]
            if (any(w in gap for w in _USAGE_WORDS) or re.match(r"\s*%", tail)
                    or re.match(r"\s*(?:\w+\s+)?(?:available|free|left|remaining|可用|剩|已用|used)", tail, re.I)
                    or re.search(r"\d\s*[–—~-]\s*$", gap)):
                continue
            mb = int(float(q.group(1)) * _UNIT_MB[q.group(2).lower()])
            if mb > best:
                best, got = mb, q
        if got is None:
            lead = _RE_LEAD.search(t[max(0, m.start() - 16):m.start()])
            if lead and not re.search(r"剩|餘|余|remaining|left|free|available|可用", t[max(0, m.start() - 24):m.start()]):
                got = lead
        if got is not None and re.match(r"[\s-]*(?:target|budget|limit|cap\b|heavy|上限|目標|目标|預算)", t[m.end():m.end() + 12]):
            got = None  # "RAM target", "RAM-heavy": a goal or a property, not a measured capacity
        if got is not None:
            gi = got.groups()
            mb = int(float(gi[-2]) * _UNIT_MB[gi[-1]])
            if mb > 0:
                out[attr] = max(out.get(attr, 0), mb)
    return out

"""Spotify連携: オーナー自身のSpotifyの再生キューに、Discordから曲を追加する。

- オーナーがBotへのDMで「音楽が聴きたい気分」等と送るとリクエスト受付ON、
  「音楽おしまい」等でOFF。ONのまま放置しても SPOTIFY_AUTO_OFF_HOURS 時間で自動OFF。
  (状態はメモリ上のみ。Botが再起動したら安全側のOFFに戻る)
- ON中は誰でも、SpotifyのトラックURLを貼るか「〇〇流して」と送ると、キューに追加される。
- 初回だけ、オーナーが「!spotify」でSpotifyアカウントを連携する(約半年ごとに再連携が必要)。

必要な環境変数:
  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET : Spotify Developer Dashboardで作成したアプリ
  SPOTIFY_OWNER_ID                          : 操作対象のSpotifyを持つ人のDiscordユーザーID
  PUBLIC_BASE_URL                           : 例 https://unamooon.duckdns.org
"""

import base64
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web

log = logging.getLogger("spotify")

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
OWNER_ID = int(os.environ.get("SPOTIFY_OWNER_ID") or 0)
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
REDIRECT_URI = f"{PUBLIC_BASE_URL}/spotify/callback"
TOKEN_FILE = os.environ.get("SPOTIFY_TOKEN_FILE", "spotify_token.json")
AUTO_OFF_HOURS = float(os.environ.get("SPOTIFY_AUTO_OFF_HOURS", "6"))
MARKET = os.environ.get("SPOTIFY_MARKET", "JP")
SCOPES = "user-modify-playback-state user-read-playback-state"


def _split_env(name: str, default: str) -> list[str]:
    return [
        unicodedata.normalize("NFKC", kw.strip())
        for kw in os.environ.get(name, default).split(",")
        if kw.strip()
    ]


# オーナーのDMでON/OFFを切り替えるキーワード(部分一致)
ON_KEYWORDS = _split_env("SPOTIFY_ON_KEYWORDS", "音楽が聴きたい気分,音楽が聞きたい気分,音楽聴きたい,音楽聞きたい,ドライブしよ")
OFF_KEYWORDS = _split_env("SPOTIFY_OFF_KEYWORDS", "音楽おしまい,音楽終わり,音楽おわり,リクエスト終了,ドライブおわり")

# 「〇〇流して」の語尾として扱う言葉
REQUEST_SUFFIXES = _split_env("SPOTIFY_REQUEST_SUFFIXES", "流して,かけて,再生して,キューに入れて,再生")

# サーバー上で曲のリクエストを受け付けるチャンネル。DMは常に受け付ける。
# 挨拶と同じチャンネル(GREETING_CHANNEL_ID)に加え、SPOTIFY_REQUEST_CHANNEL_IDS
# (カンマ区切り)で追加のチャンネルも指定できる。
REQUEST_CHANNEL_IDS = {
    int(x)
    for x in (
        os.environ.get("SPOTIFY_REQUEST_CHANNEL_IDS", "").split(",")
        + [os.environ.get("GREETING_CHANNEL_ID", "")]
    )
    if x.strip()
}


def _is_request_channel(message: discord.Message) -> bool:
    if isinstance(message.channel, discord.DMChannel):
        return True
    return message.channel.id in REQUEST_CHANNEL_IDS

TRACK_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[A-Za-z-]+/)?track/|spotify:track:)([A-Za-z0-9]{22})"
)
SHORT_URL_RE = re.compile(r"https?://(?:spotify\.link|spoti\.fi)/[A-Za-z0-9_-]+")
OTHER_SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[A-Za-z-]+/)?(?:album|playlist|artist|episode|show)/"
)
_MENTION_RE = re.compile(r"<@!?\d+>")
_REQUEST_RE = re.compile(
    r"^(.+?)\s*(?:を)?\s*(?:" + "|".join(map(re.escape, REQUEST_SUFFIXES)) + r")[\s!！。〜~ー♪]*$"
)


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and OWNER_ID and PUBLIC_BASE_URL)


# ----------------------------------------------------------------------
# ON/OFF状態
# ----------------------------------------------------------------------
_on_until: float | None = None


def is_on() -> bool:
    return _on_until is not None and time.time() < _on_until


def _turn_on() -> None:
    global _on_until
    _on_until = time.time() + AUTO_OFF_HOURS * 3600


def _turn_off() -> None:
    global _on_until
    _on_until = None


# ----------------------------------------------------------------------
# トークン管理
# ----------------------------------------------------------------------
_access_token: str | None = None
_access_token_expires_at = 0.0
_pending_states: dict[str, float] = {}  # 連携用のstate -> 期限


class SpotifyError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind  # not_linked / no_device / premium / rate_limited / other


def _load_refresh_token() -> str | None:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f).get("refresh_token")
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("Spotifyトークンファイルの読み込みに失敗しました")
        return None


def _save_refresh_token(refresh_token: str) -> None:
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": refresh_token, "saved_at": int(time.time())}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)


def is_linked() -> bool:
    return _load_refresh_token() is not None


def _basic_auth_header() -> dict:
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def _request_token(data: dict) -> dict:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://accounts.spotify.com/api/token", data=data, headers=_basic_auth_header()
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                raise SpotifyError(
                    "not_linked" if body.get("error") == "invalid_grant" else "other",
                    f"token status={resp.status} body={body}",
                )
            return body


async def _get_access_token(force_refresh: bool = False) -> str:
    global _access_token, _access_token_expires_at
    if not force_refresh and _access_token and time.time() < _access_token_expires_at - 60:
        return _access_token
    refresh_token = _load_refresh_token()
    if not refresh_token:
        raise SpotifyError("not_linked")
    body = await _request_token({"grant_type": "refresh_token", "refresh_token": refresh_token})
    _access_token = body["access_token"]
    _access_token_expires_at = time.time() + int(body.get("expires_in", 3600))
    if body.get("refresh_token"):  # 新しいものが返ってきたら差し替える
        _save_refresh_token(body["refresh_token"])
    return _access_token


async def _api(method: str, path: str, params: dict | None = None) -> tuple[int, dict | None]:
    """Spotify Web APIを呼ぶ。期限切れなら1回だけトークンを更新してやり直す。"""
    for attempt in range(2):
        token = await _get_access_token(force_refresh=attempt > 0)
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                "https://api.spotify.com/v1" + path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                text = await resp.text()
                data = None
                if text:
                    try:
                        data = json.loads(text)
                    except ValueError:
                        data = None
                if resp.status == 401 and attempt == 0:
                    continue
                if resp.status == 429:
                    raise SpotifyError("rate_limited", text)
                return resp.status, data
    return 401, None


# ----------------------------------------------------------------------
# 曲の特定とキュー追加
# ----------------------------------------------------------------------
def _describe(track: dict) -> str:
    name = track.get("name") or "(曲名不明)"
    artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
    return f"**{name}**" + (f" / {artists}" if artists else "")


async def _get_track(track_id: str) -> dict | None:
    try:
        status, data = await _api("GET", f"/tracks/{track_id}", {"market": "from_token"})
        return data if status == 200 else None
    except SpotifyError:
        raise
    except Exception:
        log.exception("曲情報の取得に失敗しました")
        return None


def _norm(s: str) -> str:
    """比較用: 全角半角・大文字小文字・空白や記号の違いを無視する。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\W_]+", "", s)


def _base_title(name: str) -> str:
    """「曲名 - New Mix」「曲名 (feat. 〇〇)」などの付け足し部分を取り除く。"""
    name = re.split(r"\s+-\s+", name or "")[0]
    return re.sub(r"[\(\[（【].*?[\)\]）】]", "", name)


def _match_score(
    track: dict, nq: str, title_hints: list[str], artist_trusted: bool = False
) -> float:
    """検索結果が、送られた文章と本当に合っているかの点数。1以上で採用。
    2   : アーティスト名も曲名も文章に含まれている
    1.5 : 文章がまるごと曲名(アーティスト指定なしの「アイドル流して」など)
    0.5 : 曲名は合っているがアーティストが違う(採用しない)
    artist_trusted: Spotify側でアーティスト指定の検索をした結果(英語表記⇔日本語表記の
    違いもSpotifyが吸収してくれている)なので、アーティストは合っているとみなす。
    """
    artists = [_norm(a.get("name", "")) for a in track.get("artists", [])]
    artist_hit = artist_trusted or any(a and a in nq for a in artists)
    t = _norm(_base_title(track.get("name", "")))
    if not t:
        return 0
    title_hit = t in nq or any(len(h) >= 2 and h in t for h in map(_norm, title_hints))
    if artist_hit and title_hit:
        return 2
    if t == nq:
        return 1.5
    return 0.5 if title_hit else 0


async def _search_items(q: str, limit: int) -> list[dict]:
    status, data = await _api(
        "GET", "/search", {"q": q, "type": "track", "limit": limit, "market": MARKET}
    )
    items = ((data or {}).get("tracks") or {}).get("items") or []
    log.info("Spotify検索: q=%r status=%s 件数=%d", q, status, len(items))
    return items if status == 200 else []


async def _search_track(query: str) -> dict | None:
    nq = _norm(query)
    # 「アーティストの曲名」の分け方の候補(曲名に「の」が入ることもあるので最大3通り)
    splits = []
    for i, ch in enumerate(query):
        if ch == "の" and 0 < i < len(query) - 1:
            artist, title = query[:i].strip(), query[i + 1:].strip()
            if artist and title:
                splits.append((artist, title))
                if len(splits) >= 3:
                    break

    searches = []
    for artist, title in splits:
        searches.append((f'track:"{title}" artist:"{artist}"', 5, True))
        # アーティスト名の表記ゆれ(空白の有無など)で上が外れても拾えるよう、曲名だけでも探す
        searches.append((title, 10, False))
    searches.append((query, 10, False))
    if splits:
        searches.append((query.replace("の", " ", 1), 10, False))

    title_hints = [t for _, t in splits] + [query]
    best, best_score, seen = None, 0.0, set()
    for q, limit, artist_trusted in searches:
        for item in await _search_items(q, limit):
            if item.get("id") in seen:
                continue
            seen.add(item.get("id"))
            score = _match_score(item, nq, title_hints, artist_trusted)
            if score > best_score:
                best, best_score = item, score
        if best_score >= 2:
            break
    if best_score < 1:
        log.info("Spotify検索: 文章に合う曲が見つかりませんでした: %r", query)
        return None
    return best


async def _add_to_queue(track_id: str) -> None:
    status, data = await _api("POST", "/me/player/queue", {"uri": f"spotify:track:{track_id}"})
    if status in (200, 202, 204):
        return
    reason = ((data or {}).get("error") or {}).get("reason", "")
    if status == 404 or reason == "NO_ACTIVE_DEVICE":
        raise SpotifyError("no_device")
    if status == 403:
        raise SpotifyError("premium", str(data))
    raise SpotifyError("other", f"queue status={status} body={data}")


async def _resolve_short_url(url: str) -> str | None:
    """spotify.link 等の短縮URLを、トラックIDに解決する。"""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                m = TRACK_URL_RE.search(str(resp.url))
                if m:
                    return m.group(1)
                body = await resp.text()
                m = TRACK_URL_RE.search(body)
                return m.group(1) if m else None
    except Exception:
        log.exception("短縮URLの解決に失敗しました: %s", url)
        return None


# ----------------------------------------------------------------------
# Discordからの入口(main.pyのon_messageから呼ぶ)
# ----------------------------------------------------------------------
async def handle_toggle(message: discord.Message) -> bool:
    """オーナーのDMでのON/OFF。処理したらTrue。"""
    if not is_configured() or message.author.id != OWNER_ID:
        return False
    if not isinstance(message.channel, discord.DMChannel):
        return False
    content = unicodedata.normalize("NFKC", message.content)
    if any(kw in content for kw in OFF_KEYWORDS):
        if is_on():
            _turn_off()
            await message.channel.send("はーい、何流そっか")
        else:
            await message.channel.send("はーい")
        return True
    if any(kw in content for kw in ON_KEYWORDS):
        if not is_linked():
            await message.channel.send(
                "Spotify教えてよ～"
            )
            return True
        _turn_on()
        hours = f"{AUTO_OFF_HOURS:g}"
        await message.channel.send(
            f"楽しみだね"
        )
        return True
    return False


async def handle_request(message: discord.Message) -> bool:
    """受付ON中の曲リクエスト。処理したらTrue(それ以外は通常処理に回す)。"""
    if not is_configured():
        return False
    if not is_on():
        return await _reply_if_request_while_off(message)
    if not _is_request_channel(message):
        return False

    content = unicodedata.normalize("NFKC", _MENTION_RE.sub("", message.content)).strip()

    track_id = None
    query = None
    m = TRACK_URL_RE.search(content)
    if m:
        track_id = m.group(1)
    elif SHORT_URL_RE.search(content):
        track_id = await _resolve_short_url(SHORT_URL_RE.search(content).group(0))
        if track_id is None:
            await message.reply("そのリンクじゃ曲が見つからなかった…", mention_author=False)
            return True
    elif OTHER_SPOTIFY_URL_RE.search(content):
        await message.reply(
            "アルバムやプレイリストはごめんね、曲のURLだけ対応してるよ",
            mention_author=False,
        )
        return True
    else:
        m = _REQUEST_RE.match(content)
        if not m:
            return False
        query = m.group(1).strip()
        if not query:
            return False

    try:
        if track_id:
            track = await _get_track(track_id)
        else:
            track = await _search_track(query)
            if track is None:
                await message.reply(
                    f"「{query}」が見つからなかった…SpotifyのURLを貼ってくれると確実だよ",
                    mention_author=False,
                )
                return True
            track_id = track["id"]
        await _add_to_queue(track_id)
    except SpotifyError as e:
        log.warning("Spotifyへの追加に失敗しました: %s", e)
        if e.kind == "no_device":
            text = "どこに追加したらいいかわかんないよ～"
        elif e.kind == "not_linked":
            text = "Spotifyとの連携が切れちゃってるみたい"
            await _notify_owner(message, "Spotifyとの連携が切れちゃってるみたい")
        elif e.kind == "rate_limited":
            text = "ちょっと混み合ってるみたい。少し待ってね"
        else:
            text = "🎵 うまく追加できなかった…"
        await message.reply(text, mention_author=False)
        return True

    desc = _describe(track) if track else "曲"
    await message.reply(f"{desc}を追加したよ", mention_author=False)
    return True


async def _reply_if_request_while_off(message: discord.Message) -> bool:
    """受付OFF中に、Bot宛て(DMかメンション)の曲リクエストが来たら「受付してない」とだけ返す。
    ここで止めないと会話AIに流れて、できたふりの返事をされてしまうため。
    Bot宛てでない(チャンネルで普通に曲を共有しているだけ等)なら何もしない。"""
    if not _is_request_channel(message):
        return False
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = message.client.user is not None and message.client.user in message.mentions
    if not (is_dm or mentioned):
        return False
    content = unicodedata.normalize("NFKC", _MENTION_RE.sub("", message.content)).strip()
    looks_like_request = (
        TRACK_URL_RE.search(content)
        or SHORT_URL_RE.search(content)
        or OTHER_SPOTIFY_URL_RE.search(content)
        or _REQUEST_RE.match(content)
    )
    if not looks_like_request:
        return False
    await message.reply("今は曲のリクエスト受け付けてないよ", mention_author=False)
    return True


async def _notify_owner(message: discord.Message, text: str) -> None:
    try:
        owner = message.client.get_user(OWNER_ID) or await message.client.fetch_user(OWNER_ID)
        await owner.send(text)
    except Exception:
        log.exception("オーナーへの通知に失敗しました")


def create_link_url() -> str:
    """オーナーが開く、Spotify連携用のURLを作る(10分有効)。"""
    now = time.time()
    for s, exp in list(_pending_states.items()):
        if exp < now:
            del _pending_states[s]
    state = secrets.token_urlsafe(24)
    _pending_states[state] = now + 600
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    }
    return "https://accounts.spotify.com/authorize?" + urlencode(params)


# ----------------------------------------------------------------------
# Webサーバー側(連携後のコールバック)
# ----------------------------------------------------------------------
def _html(text: str, status: int = 200) -> web.Response:
    return web.Response(
        text=f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<p style='font-family:sans-serif;font-size:1.1em;padding:1em'>{text}</p>",
        content_type="text/html",
        status=status,
    )


async def handle_callback(request: web.Request) -> web.Response:
    state = request.query.get("state", "")
    exp = _pending_states.pop(state, None)
    if exp is None or exp < time.time():
        return _html("リンクの有効期限が切れています。Discordでもう一度 !spotify を実行してください。", 400)
    if "error" in request.query:
        return _html("連携がキャンセルされました。", 400)
    code = request.query.get("code")
    if not code:
        return _html("連携に失敗しました。", 400)
    try:
        body = await _request_token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
        )
    except Exception:
        log.exception("Spotifyのトークン取得に失敗しました")
        return _html("連携に失敗しました。時間をおいてもう一度お試しください。", 500)
    global _access_token, _access_token_expires_at
    _save_refresh_token(body["refresh_token"])
    _access_token = body["access_token"]
    _access_token_expires_at = time.time() + int(body.get("expires_in", 3600))
    log.info("Spotifyと連携しました")
    return _html("Spotifyとの連携が完了！このページは閉じてOKだよ")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/spotify/callback", handle_callback)

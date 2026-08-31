"""The browser-only front door for the community awewarm hub."""

from html import escape
from urllib.parse import parse_qs

CONTACT_EMAIL = "peng@wehuman.top"


def wants_html(accept):
    return isinstance(accept, str) and "text/html" in accept


def pick_language(query, accept_language):
    explicit = parse_qs(query).get("lang", [None])[0]
    if explicit in {"en", "zh"}:
        return explicit
    primary = (accept_language or "").split(",", 1)[0].strip().lower()
    return "zh" if primary.startswith("zh") else "en"


CSS = """
:root { --night:#0b1220; --panel:#111c2d; --panel-2:#16243a; --ink:#edf3f5;
  --muted:#91a3b5; --line:#263850; --ember:#ffb454; --hot:#ff704b; --mint:#9ed9c3; }
* { box-sizing:border-box; margin:0; padding:0; }
body { min-height:100vh; padding:24px; color:var(--ink); background:
  radial-gradient(800px 500px at 78% 8%, rgba(255,112,75,.16), transparent 64%),
  radial-gradient(700px 420px at 12% 92%, rgba(158,217,195,.10), transparent 62%), var(--night);
  font-family: ui-rounded, "SF Pro Rounded", "PingFang SC", system-ui, sans-serif; }
.shell { max-width:960px; margin:0 auto; padding:30px 0 22px; }
header { display:flex; align-items:center; justify-content:space-between; gap:20px; }
.brand { display:flex; align-items:center; gap:12px; color:var(--ink); text-decoration:none; font-weight:800; letter-spacing:-.03em; }
.mark { width:38px; height:38px; position:relative; border:1px solid rgba(255,180,84,.6); border-radius:50%; }
.mark:before,.mark:after { content:""; position:absolute; border-radius:50%; border:1px solid var(--hot); }
.mark:before { inset:7px; border-color:var(--ember); }
.mark:after { width:5px; height:5px; right:4px; top:5px; background:var(--ember); border:0; box-shadow:-24px 24px 0 var(--mint); }
.brand-name { font-size:23px; } .brand-tag { color:var(--muted); font-size:12px; font-weight:600; letter-spacing:.12em; text-transform:uppercase; }
.language { color:var(--muted); font-size:13px; } .language a { color:var(--mint); text-decoration:none; } .language .on { color:var(--ink); font-weight:800; } .language .sep { padding:0 8px; color:var(--line); }
.hero { display:grid; grid-template-columns:1.1fr .9fr; gap:46px; align-items:center; padding:92px 0 84px; }
.eyebrow { color:var(--ember); font-size:12px; font-weight:800; letter-spacing:.18em; text-transform:uppercase; margin-bottom:20px; }
h1 { max-width:620px; font-size:clamp(42px,7vw,76px); line-height:.98; letter-spacing:-.065em; }
h1 em { color:var(--ember); font-style:normal; } .lede { max-width:560px; margin-top:24px; color:var(--muted); font-size:17px; line-height:1.75; }
.hero-links { display:flex; flex-wrap:wrap; gap:12px; margin-top:32px; }
.button { display:inline-flex; align-items:center; padding:12px 17px; border-radius:8px; color:var(--night); background:var(--ember); font-size:14px; font-weight:800; text-decoration:none; }
.button.alt { color:var(--ink); background:transparent; border:1px solid var(--line); } .button:hover { filter:brightness(1.08); }
.orbit-card { min-height:315px; display:grid; place-items:center; position:relative; border:1px solid var(--line); border-radius:22px; background:linear-gradient(145deg,rgba(22,36,58,.96),rgba(12,19,32,.9)); overflow:hidden; }
.orbit-card:before { content:""; position:absolute; width:280px; height:280px; border:1px solid rgba(255,180,84,.26); border-radius:50%; box-shadow:0 0 0 28px rgba(255,180,84,.035), 0 0 0 72px rgba(255,180,84,.025); }
.orbit { position:relative; width:178px; height:178px; border:1px dashed rgba(158,217,195,.45); border-radius:50%; animation:spin 24s linear infinite; }
.orbit:before { content:""; position:absolute; inset:30px; border:1px solid rgba(255,112,75,.45); border-radius:50%; }
.sun { position:absolute; inset:61px; border-radius:50%; background:radial-gradient(circle at 35% 30%,#ffd891,var(--hot)); box-shadow:0 0 36px rgba(255,112,75,.6); }
.spark { position:absolute; width:9px; height:9px; border-radius:50%; background:var(--mint); box-shadow:0 0 16px var(--mint); } .spark.one { top:-4px; left:82px; } .spark.two { right:2px; bottom:35px; background:var(--ember); box-shadow:0 0 16px var(--ember); }
.orbit-label { position:absolute; bottom:22px; color:var(--muted); font:12px ui-monospace,monospace; letter-spacing:.12em; }
@keyframes spin { to { transform:rotate(360deg); } }
.section-head { display:flex; align-items:end; justify-content:space-between; border-bottom:1px solid var(--line); padding-bottom:16px; margin-bottom:18px; } h2 { font-size:25px; letter-spacing:-.04em; } .section-head span { color:var(--muted); font-size:12px; }
.grid { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; } .tile { min-height:198px; padding:23px; border:1px solid var(--line); border-radius:14px; background:rgba(17,28,45,.72); } .tile .num { color:var(--ember); font:700 12px ui-monospace,monospace; } .tile h3 { margin:31px 0 10px; font-size:18px; } .tile p { color:var(--muted); font-size:14px; line-height:1.65; }
.connect { display:grid; grid-template-columns:.8fr 1.2fr; gap:28px; margin-top:72px; padding:28px; border:1px solid rgba(255,180,84,.34); border-radius:16px; background:linear-gradient(120deg,rgba(255,180,84,.09),rgba(17,28,45,.65)); } .connect h2 { color:var(--ember); } .connect p { margin-top:12px; color:var(--muted); line-height:1.7; font-size:14px; } .connect a { color:var(--mint); font-weight:700; }
pre { overflow:auto; padding:18px 20px; border-radius:10px; background:#080e18; color:#d9e5e4; font:13px/1.8 ui-monospace,SFMono-Regular,Menlo,monospace; } pre .prompt { color:var(--ember); } pre .value { color:var(--mint); }
.trust { margin-top:16px; color:var(--muted); font-size:12px; line-height:1.6; } .trust strong { color:var(--ink); }
footer { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-top:64px; padding-top:18px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; } footer a { color:var(--mint); text-decoration:none; }
@media (max-width:720px) { body { padding:16px; } .shell { padding-top:16px; } .hero { grid-template-columns:1fr; gap:34px; padding:68px 0 58px; } .orbit-card { min-height:240px; } .grid,.connect { grid-template-columns:1fr; } .connect { margin-top:54px; padding:22px; } }
"""


def landing_html(lang, host, version):
    host = escape(host or "localhost", quote=True)
    email = escape(CONTACT_EMAIL, quote=True)
    zh = lang == "zh"
    if zh:
        title = "让订阅窗口一直保持温热。"
        headline = "让订阅窗口一直保持温热<em>。</em>"
        lead = "awewarm 是一个轻量的后台调度器：在正确的时间发送一条最小请求，让 Claude Code、Codex 和兼容套餐的用量窗口持续开启。"
        docs = "阅读文档"; github = "查看 GitHub"; why = "为什么需要它"; why_tag = "A SMALL REQUEST, RIGHT ON TIME"
        cards = [("01", "机器可以睡觉", "fixed 模式配合补跑窗口，合盖、短暂断网也不必手动盯着。"), ("02", "窗口滚动续期", "已确认窗口时长后，用 interval 模式按窗口 + 缓冲自动续期。"), ("03", "把保温交给 Hub", "没有常开机器？通过邀请制社区 Hub，让常驻服务器替你发送请求。")]
        connect_title = "加入社区 Hub"; connect_copy = f"申请一次性邀请码，然后把自己的 awewarm 连接委托给这台常驻服务器。邀请制入口：<a href=\"mailto:{email}\">{email}</a>。"
        command = f'<span class="prompt">$</span> awewarm remote connect https://{host} \\\n    --invite <span class="value">awi_…</span>'
        trust = "<strong>信任边界：</strong>委托到 Hub 的 API key 会经过运营者机器的内存；默认不会落盘。"
        community = "社区 Hub 指南"; lang_toggle = '<a href="/?lang=en">EN</a><span class="sep">/</span><span class="on">中文</span>'
    else:
        title = "Keep the window warm."
        headline = "Keep the window warm<em>.</em>"
        lead = "awewarm is a small background scheduler that sends one minimal request at the right moment, keeping Claude Code, Codex, and compatible coding-plan windows open."
        docs = "Read the docs"; github = "View on GitHub"; why = "Why awewarm"; why_tag = "A SMALL REQUEST, RIGHT ON TIME"
        cards = [("01", "Your machine can sleep", "fixed mode and catch-up windows handle lids, brief outages, and missed slots."), ("02", "Renew on a rolling clock", "Once a window is confirmed, interval mode renews it after window + a small buffer."), ("03", "Delegate to a Hub", "No always-on machine? An invite-only community Hub can fire the requests for you.")]
        connect_title = "Join the community Hub"; connect_copy = f"Request a one-time invite, then delegate your awewarm connection to this always-on server. Invite-only: <a href=\"mailto:{email}\">{email}</a>."
        command = f'<span class="prompt">$</span> awewarm remote connect https://{host} \\\n    --invite <span class="value">awi_…</span>'
        trust = "<strong>Trust boundary:</strong> an API key delegated to this Hub passes through the operator's machine memory; it is RAM-only by default."
        community = "Community Hub guide"; lang_toggle = '<span class="on">EN</span><span class="sep">/</span><a href="/?lang=zh">中文</a>'
    tiles = "".join(f'<article class="tile"><span class="num">{n}</span><h3>{h}</h3><p>{p}</p></article>' for n, h, p in cards)
    return f'''<!doctype html><html lang="{"zh-CN" if zh else "en"}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>awewarm · {title}</title><style>{CSS}</style></head><body><main class="shell"><header><a class="brand" href="/"><span class="mark"></span><span class="brand-name">awewarm</span><span class="brand-tag">hub</span></a><nav class="language">{lang_toggle}</nav></header><section class="hero"><div><div class="eyebrow">{why_tag}</div><h1>{headline}</h1><p class="lede">{lead}</p><div class="hero-links"><a class="button" href="https://github.com/wehuman01/awewarm">{github}</a><a class="button alt" href="https://github.com/wehuman01/awewarm/blob/main/docs/community-hub/README{"_cn" if zh else ""}.md">{docs}</a></div></div><div class="orbit-card"><div class="orbit"><span class="sun"></span><span class="spark one"></span><span class="spark two"></span></div><span class="orbit-label">WINDOW / WARM / READY</span></div></section><section><div class="section-head"><h2>{why}</h2><span>01 — 03</span></div><div class="grid">{tiles}</div></section><section class="connect"><div><h2>{connect_title}</h2><p>{connect_copy}</p><p class="trust">{trust}</p></div><pre>{command}</pre></section><footer><span>awewarm-hub v{escape(version)}</span><span><a href="https://github.com/wehuman01/awewarm-hub">GitHub</a> · <a href="https://github.com/wehuman01/awewarm/blob/main/docs/community-hub/README{"_cn" if zh else ""}.md">{community}</a> · <code>GET /healthz</code></span></footer></main></body></html>'''

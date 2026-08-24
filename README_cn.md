<div align="center">
  <img src="logo/hero2.webp" alt="awewarm-hub" width="860">
  <h1>awewarm-hub:多租户 Hub 服务器</h1>
  <p><strong>一台常驻服务器，让整个团队的订阅窗口一直是热的。</strong></p>
  <p><a href="https://github.com/wehuman01/awewarm">awewarm</a> 的多租户 hub 服务器：多个用户、一台服务器、一次性邀请码 —— 每个用户的密钥始终留在自己的机器上。</p>
  <p>
    <a href="./README.md">English</a> ·
    <strong>简体中文</strong>
  </p>
  <p>
    <a href="https://ko-fi.com/mugpeng"><img src="https://img.shields.io/badge/Ko--fi-Buy%20me%20a%20coffee-FF5E5B?style=flat-square&logo=ko-fi&logoColor=white" alt="Ko-fi"></a>
  </p>
  <p>
    <img src="https://img.shields.io/pypi/v/awewarm-hub?style=flat-square&color=7C3AED" alt="Version">
    <img src="https://img.shields.io/badge/python-%E2%89%A5%203.9-0EA5E9?style=flat-square" alt="Python">
    <img src="https://img.shields.io/badge/license-MPL--2.0-22C55E?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/status-alpha-c96a3d?style=flat-square" alt="Status">
    <img src="https://img.shields.io/badge/install-pip-22C55E?style=flat-square" alt="pip install">
    <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-334155?style=flat-square" alt="Platform">
    <img src="https://img.shields.io/pepy/dt/awewarm-hub?style=flat-square" alt="PyPI downloads">
    <img src="https://img.shields.io/github/stars/wehuman01/awewarm-hub?style=flat-square" alt="GitHub stars">
  </p>
</div>

> 真实场景：五人团队共用一台 5 美元的 VPS。每个人的订阅套餐在那台永不休眠的机器上全天候保温 —— 而所有 API key 仍留在各自的笔记本电脑上。

awewarm-hub 把角色一分为二：

- **运维者**（这台机器，7×24 常驻盒子）：运行 `awewarm-hub serve` 和下述管理命令，签发一次性邀请码并分发给成员。
- **用户**（各自的机器，只需开源的 [awewarm](https://github.com/wehuman01/awewarm)）：`awewarm remote connect <url> --invite awi_...`，然后 `awewarm config set <id> --remote`。他们完全不需要本包。

## 安装

需要 Python ≥ 3.9:

```bash
pip install awewarm-hub          # 会一并装上 awewarm
```

## 快速开始

### 让 AI agent 代装

在 Claude Code、Codex 或其他编程 agent 里,对它说:

```text
阅读 https://github.com/wehuman01/awewarm-hub/blob/main/README.ai.md 并按其指引搭建 awewarm-hub 服务器。
```

Agent 会安装 CLI、只读检查状态与租户,并按你的要求签发邀请码。常驻的 `serve` 进程本身留在你的终端(或 systemd)里 —— agent 绝不把它后台化。

### 手动搭建

```bash
awewarm-hub serve                # 监听 127.0.0.1:8790,数据在 ~/.awewarm-server
awewarm-hub invite --name alice  # 打印 awi_...(一次性,48 小时有效)
awewarm-hub status               # 容量、邀请码、租户、serve 存活状态
```

## awewarm 与 awewarm-hub

两个包、两种角色,同样的 [MPL-2.0](LICENSE) 许可证、同一个 [wehuman01](https://github.com/wehuman01) 组织:

| 包 | 谁安装 | 负责什么 |
| --- | --- | --- |
| [awewarm](https://github.com/wehuman01/awewarm) | 所有用户(各自机器) | 调度保温请求;`awewarm serve` 覆盖单人服务器场景 |
| **awewarm-hub**(本包) | 仅运维者(7×24 盒子) | 多租户服务:租户、一次性邀请码、配额、revoke/restore |

底层引擎(WarmServer、schedule、transport、HTTP handler 核心)来自 `awewarm` pip 依赖,并锁定其 minor 版本,保证与开源客户端的通信协议始终同频。与单人 awewarm 服务器完全一样,通过 cloudflared 隧道对外暴露(免费 TLS、无需开放入站端口、源站 IP 不暴露)—— 参见 awewarm README 的 *Remote Server* 一节。systemd 用户单元同款结构,只是 `ExecStart=awewarm-hub serve`:

```ini
[Unit]
Description=awewarm-hub serve
After=network-online.target

[Service]
ExecStart=awewarm-hub serve
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now awewarm-hub`(无桌面/SSH 环境先执行 `loginctl enable-linger $USER`)。

## 命令

```bash
awewarm-hub serve [--data-dir/--bind/--port]   # 常驻 hub 服务器
                 [--max-tenants/--max-conns-per-tenant/--max-machines/--tick-seconds]
awewarm-hub status [--details]                 # 容量、邀请码数量、租户、serve 存活状态
awewarm-hub invite [--name <who>] [--count N] [--expires-in 30m|12h|7d] [--machines N]  # 铸造(裸形式)
awewarm-hub list users [--api|--reveal|--json] # 租户:健康度、用量、机器、加入时用的邀请码
awewarm-hub list invites [--reveal|--token|--json]  # 所有已签发邀请码:待用/已用/已吊销/已过期,及其机器上限;--token 附带其租户的 token
awewarm-hub invite rename <awi_...> <name>     # 重命名邀请码的备注;已用的连其租户一并改名
awewarm-hub invite revoke <awi_...>            # 作废一个邀请码:待用的立即失效,已用的停用其租户(可逆)
awewarm-hub invite revoke <awi_...> --delete   # 从台账中彻底删除该行;已用的连其租户一并删除(不可逆)
awewarm-hub invite restore <awi_...>           # 撤销一次 revoke
awewarm-hub invite extend <awi_...> --expires-in 7d  # 把过期时间从现在起往后推；已过期的码重新可用
awewarm-hub config [--data-dir /data|--unset]  # 本机默认数据目录
awewarm-hub config --max-tenants 20 [--max-conns-per-tenant N] [--max-machines N] [--reset]
                                               # 容量上限;运行中的 serve 无需重启即采纳
awewarm-hub config --persist-keys on|off       # 是否允许租户把 API key 落盘到本机
                                               #   默认 off;切回 off 会一次性清空所有已存 key
awewarm-hub self-update [--check]              # 从 PyPI 升级
```

## 工作原理

每个租户在 `tenants/<id>/` 下拥有私有工作区(连接、状态、内存密钥环 —— 租户之间互相不可见)。`tenants.json` 以明文保存邀请码和租户 token,方便运维者找回已发出去的任意一个(`list invites --reveal` / `--token`);认证时比对 token 的 SHA-256 哈希,因此重启后配对关系依然有效。用户弄丢了 token,运维者把它交回,用户用 `awewarm remote connect <url> --token <它>` 重连 —— 还是同一个租户、同一批连接。任何能读到数据目录的人都能花掉待用邀请码或冒充租户,请妥善保管。邀请码是授权的唯一台账:`revoke awi_...` 作废一个待用码,或停用它产出的租户(token 被拒、连接不再被调度、容量名额释放),`restore awi_...` 反向操作 —— 机器配对不受影响,往返完全无损。`revoke awi_... --delete` 则把该行从台账中彻底删除:不留吊销墓碑,已用的连其租户一并删除(token 失效、容量名额释放、工作区保留在磁盘上),`restore` 无法找回;已吊销的行同样可以 --delete —— 即清理墓碑的场景。机器上限在铸造时盖进邀请码(`invite --machines N`,缺省取 `max-machines` 上限);要给在线用户加机器额度,改 `tenants.json` 里其邀请码行的 `machines` 值(运行中的 serve 会采纳磁盘改动),或直接发新码。三个容量上限都存放在 registry 的 serve 记录里:serve 参数在启动时盖戳,`awewarm-hub config --max-tenants 20` 随时可改 —— 运行中的 serve 无需重启即采纳(每次租户操作和每个调度 tick 都会重读该记录)。轻微的每租户限速(60 请求/分钟)可拦截循环请求的客户端。

一条信任规则,直说:hub 用用户的 API key 发送请求,因此明文 key 会经过它的内存。Hub 适合信任机器运维者(和 root)的人;和陌生人共享 VPS 不属于这种情况。

API key **默认**永远不落盘 —— 只存在于服务器内存中,重启后由每个用户的机器重新推送。(邀请码和租户 token 是上述刻意的磁盘例外,为找回而保留;请保管好数据目录。)

对机器长期离线的用户另有一个可选项:运维者先 `awewarm-hub config --persist-keys on`(默认关闭,开启时警告),用户再按连接确认(`awewarm config set <id> --persist-key on`),key 便会存入其工作区的 `keys.json`(明文,0600),hub 重启后无需补推、保温不断。任一侧随时可撤:用户关掉开关(hub 立即删除该 key)、运维者切回 off(盒上所有已存 key 一次性清空)、吊销或删除邀请码(该租户的 key 一并清除 —— 工作区其余照旧保留)。该功能设计上就不鼓励;全部保持关闭是推荐姿态。

## 安全性

给用户的一句话版本:所有常规泄漏路径都已被设计堵死;唯一剩下的,是信任 hub 所在的那台机器。

- **账户登录信息永远不离开你的机器。** hub 只接受 API key(订阅)连接;CLI 账号(OAuth)凭据在协议层即被拒绝,留在你登录它的那台机器上。不存在可泄漏的用户名、密码或会话 cookie。
- **你的 API key 在 hub 上只存在于内存**(除非你主动选择 `--persist-key` —— 不建议;那时它以明文存在你工作区的 `keys.json` 里,0600,你或运维者任一方撤回即消失)。从不进日志(激活结果不含 key 和认证头),也没有任何端点能把它读回 —— `/v1/state` 只报告 key 是否在位。流量全程走运维者的 HTTPS 隧道。
- **租户 token 被盗 ≠ API key 被盗。** 拿到你的 `awt_...` 的人能管理和触发**你的**连接,但读不到已存储的 key,也无法把它发到自己的服务器 —— 替换连接必须推送新 key,而新 key 会覆盖旧的。token 泄漏了:请运维者 `revoke` 你的邀请码(token 立即失效),拿新码重新配对;你的机器会重新推送一切。
- **租户之间互相不可见** —— 私有工作区、常数时间哈希比对、每租户 60 请求/分钟限速。

唯一的边界,直说:hub 用你的 key 发送保温请求,明文 key 会经过 hub 进程的内存。运维 hub 的人 —— 或持有该机器 root 的人 —— 能读到它。以上各条都是设计保证;唯独这一条是信任决策,即[工作原理](#工作原理)里的同一条规则:hub 的可信度等于它所在机器的可信度。

建议:

- 用户:委托一把专用的、可随时撤销的 key —— 不要和你其他工具共用那把 —— 有疑虑就轮换。
- 用户:只通过 hub 的 `https://` 地址连接,绝不裸连 `http://host:port`;保持客户端包更新。
- 运维者:只通过 cloudflared 隧道(或等效 TLS 反代)暴露 hub,保持默认 `--bind 127.0.0.1`,保管好数据目录(`tenants.json` 明文保存邀请码和租户 token,是刻意设计),保持本包更新。

## 从拆分前的 `awewarm serve --hub` 升级

数据目录(`~/.awewarm-server`,或 `--data-dir`/旧 `hub config --data-dir` 设置的路径)原样沿用 —— 租户、邀请码、已持久化的数据目录设置全部继续工作。停掉旧的 serve,安装本包,启动 `awewarm-hub serve` 即可。旧写法(`awewarm serve --hub`、`awewarm hub ...`)在 awewarm 中以墓碑形式提示改用这里的对应命令。

升级到 v0.5.6(破坏性变更):`revoke`/`restore` 只按邀请码寻址 —— `revoke t_...` 已删除;租户加入时用的码用 `list invites --reveal` 查。升级后首次启动会一次性迁移 `tenants.json`:租户上的挂起状态移到其邀请码的 `revokedAt`;明文码落盘之前铸造的老邀请行会连同它产出的租户一起删除(其 token 随之失效;工作区保留在磁盘上,发新码重新配对即可)。请同时升级本包并重启 serve,让两个进程说同一版台账格式。

## 配置

`awewarm-hub config` 是设置入口 —— 一个命令管两件事。不带参数时展示当前生效值及其来源:数据目录和三个容量上限。`--data-dir /data` 持久化本机默认数据目录(`--unset` 清除);与容量参数同时给出时,它变为一次性的 registry 选择器(与其它命令上的 `--data-dir` 一致),不再持久化。默认为 `~/.awewarm-server`,与 awewarm 的单人服务器共用。目录内:`tenants.json`(token 哈希、邀请码、含容量/监听地址/版本/启动时间的 serve 记录)和每个租户一个私有 `tenants/<id>/` 工作区。

容量上限 —— `--max-tenants`、`--max-conns-per-tenant`、`--max-machines`(`--reset` 清除,回到 10/5/1 默认)—— 存放在 serve 记录里,所有进程读同一处:serve 参数在启动时盖戳,`config` 随时可改,运行中的 serve 无需重启即采纳(每次租户操作和每个调度 tick 都会重读该记录;一次性 CLI 进程读同一组数字,首次启动 serve 之前设置的值同样生效)。

## 自更新

```bash
awewarm-hub self-update            # 升级到最新发布版
awewarm-hub self-update --check    # 只查看版本
```

## 开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ../awewarm -e .     # 开源引擎(editable)+ 本包
python3 -m unittest discover -s tests
```

在本检出目录运行时,`awewarm-hub -v` 会显示 `editable`(附 git 状态);pip 记录的元数据冻结在 `pip install -e .` 时刻,版本号变更后请重新执行以保持 `pip show` 同步。`awewarm-hub self-update` 在源码检出上会拒绝执行 —— 请 git pull 后重装。

源码仓库为 [wehuman01/awewarm-hub](https://github.com/wehuman01/awewarm-hub)(开源,MPL-2.0);tag 推送时自动构建并发布到 PyPI。工程准则见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md),发布历史见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

## 支持

如果 awewarm-hub 保住了你们团队的配额,欢迎支持:

- ⭐ Star 本仓库 —— 让更多人看到它。
- ☕ [Ko-fi](https://ko-fi.com/mugpeng) —— 请我喝杯咖啡。
- 💬 微信 —— 扫描下方二维码。

<p align="center">
  <img src="assets/images/wechat-pay.jpg" alt="WeChat Pay" width="240">
</p>

> awewarm-hub 免费开源。赞助让它得以持续维护 —— 谢谢你。

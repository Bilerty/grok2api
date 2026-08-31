# 丢家宽给 AI：装成 Grok2API lab 同款

**主路径是本 fork（lij768423-svg/grok2api）+ Quality Guard sidecar。不要装 CPA，不要只开一个节点，不要 pull 官方 latest 镜像。**

用户只需要把全部家宽 sticky 丢给你。你负责：拆 session → **先让用户确认出口架构（直连 / Mihomo 中转，见步骤 0，二选一，不要默认）** → 每条 sticky 一个出口配置 + 一个 Grok2API 节点 → 打开 Guard。

这个 fork 开箱即用：官方最新 + **缺思考 / 假加密思考拦截默认开**。`docker compose up -d --build` 会带 sidecar。

---

## 0. 用户复制这段发给任何 AI

```text
按这个文档装，不要发挥：
https://github.com/lij768423-svg/grok2api/blob/main/AI_GROK2API_INSTALL.md

主路径：lij768423-svg/grok2api（fork）+ Quality Guard sidecar。不要装 CPA。
不要 pull ghcr.io/chenyme/grok2api:latest，必须 clone 本 fork 再 docker compose up -d --build。

这个 fork 开箱即用：官方最新 + 缺思考拦截默认开。
- hold 30s / minOutput 8 / 6 枪 / fail_closed
- 短 encrypted_content stub 不算思考；floor = max(256B, reasoning_tokens×4)
- hold 到期后的短问候 + 高 reasoning（「你好」）继续扣；floor 达标但 1 秒内吐短回复也扣
- TUI 续聊（previous_response_id）和 hosted tools 仍 hold，扣住后换号
- 缺思考冷却 12h，空流 15m；docker compose up -d 带 sidecar

家宽全部用上，每个 sticky 一个出口配置 + 一个 Grok2API 节点。
出口架构先问我：直连（不装 Mihomo，proxy-profile 直填供应商链接）还是 Mihomo 中转。二选一，不要默认。
禁止只开 1 个交差。禁止把多条合成一个「住宅池」。

机器：Linux + Docker，装到 ~/grok-stack（新目录，别覆盖现网）。
有邮箱再一起装注册机；没有也行，先把出口和 Guard 拉起来。

家宽（一行一条，URL / host:port:user:pass / 带 sid 都行）：

```

用户在后面贴家宽。贴完你就开干（出口架构那一个问题要先问，见步骤 0），不要再问「要不要先单节点验证」。

---

## 1. 你先读什么

1. 本文（落地顺序）
2. 本仓库 `config.example.yaml`（`requestRetry` 已开）
3. 本仓库 `tools/egress-quality-guard/README.zh-CN.md`
4. 家宽脚本：方案 B 用 `scripts/from_residential.py`；方案 A 用 `scripts/from_residential_direct.py`（解析 + 建 profile/节点 + 测出口，一条龙）

不要走 CPA 插件。那是旁路，不是交付。

---

## 2. 用户只需要给这些

| 必给 | 说明 |
| --- | --- |
| 一台能跑 Docker 的 Linux | 新目录，默认 `~/grok-stack` |
| **全部家宽** | 每一条 sticky 都贴出来 |

| 有就给，没有就先空着 | 说明 |
| --- | --- |
| 邮箱 | 要注册机才要。只想先把出口和 Guard 拉起来可以后补 |
| 已有 Grok2API | 说路径。没说 = 按本 fork 新建，禁止改别人现网 compose |

家宽一行一条，下面都能认：

```text
http://USER:PASS@HOST:PORT
socks5://USER:PASS@HOST:PORT
USER:PASS@HOST:PORT
HOST:PORT:USER:PASS
节点名 | http://USER:PASS@HOST:PORT
http://ACCOUNT-region-US-sid-XXXX-t-10:PASS@HOST:PORT
```

**同一 host:port、不同 username / sid = 不同 session = 不同节点。**
用户写「我买了 8 条」但只贴 1 条 URL：停下来让他把 sid 列表或 8 行都贴全。不准自行复制成 8 个假节点。

聊天里解析完只回：名称、（方案 B 的）listener 端口、出口 IP。不要回显完整代理 URL / 密码。

---

## 3. 硬性规则（违反 = 没装完）

1. 用户贴了 N 条家宽，交付必须有 **N 个独立出口 + N 个 Grok2API 节点**（方案 A：N 个 proxy-profile + N 个节点；方案 B：N 个 listener + N 个节点）。
2. 使用侧节点数 **< 3** 只能叫冒烟，汇报里必须写「不像 lab」。
3. 使用侧 **≥ 3** 才允许说接近 lab：Guard `passive` + `failClosed: true` + `softTPS: 200`。
4. 禁止：只加 `res-01`、把 8 条 sticky 合成一个节点、`proxyPool=true` 套在 sticky 上、阶段 A 停住交差。
5. （仅方案 B）禁止：Grok2API 在 bridge 网络里填 `http://127.0.0.1:端口`（那是容器自己）。用 `network_mode: host`，或 `host.docker.internal` / 宿主机网关。
6. 注册口和使用口不要抢同一条 sticky。脚本会在 N≥4 时自动拆（拆分逻辑在 `from_residential.py`，属方案 B 流程；方案 A 没有「注册口 scope」，全部 sticky 做使用侧）。
7. 不要把 CPA `.so`、8317、商店插件写进交付说明。
8. 不要 `docker pull ghcr.io/chenyme/grok2api:latest`。官方镜像 **默认不拦截**，也没有本 fork 的密文 floor / burst。必须 `--build` 本仓库。

---

## 4. 落地顺序（不可跳）

### 0. 出口架构确认点（先问用户，二选一，不要默认）

| | 方案 A：直连（不装 Mihomo） | 方案 B：Mihomo 中转（原默认路径） |
| --- | --- | --- |
| 填法 | proxy-profile 直接填供应商 `socks5h://` 完整链接，每个节点绑定一个 profile | 每条 sticky 一个本地 listener，节点只填 listener 地址 |
| 优点 | 少一个常驻组件与 systemd 服务；部署/排障简单；compose 不用处理容器可达性；少一层转发 | 供应商账密不进 grok2api（节点只见本地地址）；协议归一成本地 http；本地端口 `curl` 3 次测出口 IP 方便；「换 IP webhook」（`session_rotator` + `rotationURL`）只有这条路径能用 |
| 缺点 | 供应商账密存进 grok2api 数据库（`credentialEncryptionKey` 加密存储，管理端 reveal 时密码打码显示）；测出口 IP 要走节点 test 接口；换 IP 自动化不可用 | 多维护一份 432 listener 的生成与进程；compose 需 `host.docker.internal` / host 网关 |
| 最终效果 | **对网关功能与防降智 Guard 完全等价** | 同左 |

等价性是代码层面的事实，不是口号：

- 后端原生支持 `socks5/socks5h`（`NormalizeProxyURL` 放行，`golang.org/x/net/proxy` 拨号），userinfo 里带账号密码合法。
- Guard 自动发现只看 `scope=grok_build + proxyConfigured + enabled`；profile 的 URL 在节点保存时已加密物化进节点行，改 profile 自动传播。探针请求由主程序进程内经节点代理发出，sidecar 只访问 `GROK2API_BASE_URL`，全程无 Mihomo 假设。
- 拦截能力（`requestRetry` / hold / 密文 floor / burst / 隔离恢复）全部在主程序与 sidecar 状态机里，两条路径一字不差。

差异只有四点：账密存放位置、出口 IP 的测量手段、换 IP 自动化、组件数量。用户不选就停下来问，不要替他决定。

### A. 解析家宽

把用户原文写到本机文件（0600），不要进 git：

```bash
umask 077
mkdir -p ~/grok-stack/egress-gen
# 用户家宽 → ~/grok-stack/residential.dump   chmod 600

git clone --depth 1 \
  https://github.com/lij768423-svg/grok2api.git \
  ~/grok-stack/grok2api

python3 ~/grok-stack/grok2api/scripts/from_residential.py \
  ~/grok-stack/residential.dump \
  --out-dir ~/grok-stack/egress-gen
```

看 `egress-gen/plan.md`。`lab_like: false` 就告诉用户再补 sticky，但仍把已有的全部开成节点。

方案 A 用不到 `mihomo.yaml`。推荐直接用直连脚本：`--dry-run` 离线解析出 `direct-plan.md`（条数、名称、脱敏链接），等 C 步服务起来后再全量跑（登录 → 建 profile/节点 → 测出口 → 写报告）。解析逻辑与 `from_residential.py` 完全同源，行格式通用：

```bash
# 离线解析（不碰 API）
python3 scripts/from_residential_direct.py ~/grok-stack/residential.dump --dry-run \
  --out-dir ~/grok-stack/egress-direct
# 服务起来后全量执行（密码用 --password-file 或 GROK2API_ADMIN_PASSWORD 传，别贴聊天里）
python3 scripts/from_residential_direct.py ~/grok-stack/residential.dump \
  --api-base http://127.0.0.1:8000 \
  --password-file ~/grok-stack/admin-password.txt \
  --out-dir ~/grok-stack/egress-direct
```

报告（`direct-report.md` / `exit-ips.json`，0600）里就是验收要的 `节点 → 出口 IP` 表。脚本按名称幂等：重跑只补缺，不会重复建。

### B. 起 Mihomo（仅方案 B；方案 A 跳过本步）

用生成的 `egress-gen/mihomo.yaml` 起一个**新的** Mihomo，不要改用户现网 `mihomo-grok-*`。

对每个 listener 测 3 次出口 IP：

```bash
curl -s --max-time 20 --proxy http://127.0.0.1:8301 https://api.ipify.org
```

记到表里（只记端口和 IP）。sticky 三次应相同。两个「节点」出口 IP 相同 = 还是一个故障域，标出来，不要假装拆开了。

### C. 起 Grok2API + Guard

```bash
cd ~/grok-stack/grok2api
./scripts/bootstrap-lab-config.sh
# 或：cp config.example.yaml config.yaml 后自己填 secrets / bootstrapAdmin
```

`config.example.yaml` 已经是 lab 默认，**不要改回官方的 `requestRetry.enabled: false` / `minOutputTokens: 32`**。核对：

```yaml
qualityGuard:
  enabled: true
  model: "grok-4.6"
  mode: passive
  requestRetry:
    enabled: true
    maxAttempts: 6
    holdTimeout: 30s
    minOutputTokens: 8
    onExhausted: fail_closed
    accountCooldown: 12h
    idleAccountCooldown: 15m
    minEncryptedBytes: 256
    encryptedBytesPerReasoningToken: 4
```

使用侧 ≥3 时，把 Guard 调到接近 lab（拦截参数保持上面这组）：

```yaml
qualityGuard:
  softTPS: 200
  failClosed: true
  minimumHealthyNodes: 2   # 脚本 guard.json 为准；≥4 用 3
```

使用侧 <3 时：`failClosed: false`、`softTPS: 500`，汇报写「不像 lab」。

Compose（编本 fork，不要 pull 官方 latest）：

```bash
docker compose up -d --build
```

本 fork **不用** `--profile quality-guard`：sidecar 默认就起。

sidecar 阈值一律以 `config.yaml` 的 `qualityGuard` 为准：主程序经共享卷 `bootstrap.json` 注入 sidecar，管理页保存的运行策略走 `runtime-config.json` 热加载。compose 里 sidecar 的 `QUALITY_GUARD_*` 环境变量和外面流传的 `RANK_SCHEDULER_ENABLED / RANK_DRY_RUN` 在本仓库代码里**没有任何消费方**——照抄不报错但也完全不生效，不要写进交付说明。

镜像本地构建（`--build`）或免构建都行；有云端镜像时在 `.env` 里覆盖（0600，不进 git）：

```text
GROK2API_IMAGE=ghcr.io/<命名空间>/grok2api:<tag>
GROK2API_QG_IMAGE=ghcr.io/<命名空间>/grok2api-quality-guard:<tag>
```

### D. 每条 sticky 一个出口节点（按步骤 0 选的路径）

**方案 A（直连）：每条 sticky 一个 proxy-profile + 一个绑定它的节点**

自动化路径就是上面的 `scripts/from_residential_direct.py`。下面是它等价的手工 API 细节（用于理解、补数或脚本失败时兜底）。

管理端 Egress 新建，或直接调 API（登录 `POST /api/admin/v1/auth/login` 拿 accessToken，后续带 `Authorization: Bearer`）：

```text
POST /api/admin/v1/egress-proxy-profiles   {name: <sticky 名称>, proxyURL: <完整 socks5h://...>}
POST /api/admin/v1/egress-nodes            {name: <同名>, scope: "grok_build", enabled: true, proxyPool: false, proxyProfileId: <上一步返回的 id>}
```

注意：`proxyProfileId` 与 `proxyURL` 互斥，节点上只绑 profile；profile 有节点绑定时不可删，改 profile 的 URL 会自动传播到全部绑定节点并重置探测状态。

出口验证用节点测试接口（一条一个测；批量口每批 ≤200）：

```text
POST /api/admin/v1/egress-nodes/<id>/test   →  {exitIp, latencyMs}
```

把 `节点 → exitIp` 记成表，同一 sticky 多次 test 的 IP 应一致。两个「节点」出口 IP 相同 = 还是一个故障域，标出来，不要假装拆开了。

**方案 B（Mihomo 中转）：每个使用侧 listener 建一个节点**

管理端 Egress，scope=`grok_build`：

| 字段 | 值 |
| --- | --- |
| 名称 | `plan.md` 里的 use 名 |
| 代理 URL | Mihomo listener（注意 Docker 可达地址） |
| `proxyPool` | **false** |

注册侧 listener 只进注册机代理池，**不要**建成使用侧 Guard 节点。

自动发现由 `config.yaml` 的 `qualityGuard.nodeIDs: []`（默认值）控制：sidecar 自动纳管全部 `scope=grok_build` 且已配代理的启用节点。不存在 `QUALITY_GUARD_NODE_IDS` 这个环境变量，别照抄旧文档。

### E. 注册机（用户给了邮箱才做；「注册口」拆分仅在方案 B 里有）

```bash
# 或 grok-fullchain ./deploy/one-click.sh
# 面板代理池只填 8201+ 注册口，不要填 8301+ 使用口（方案 A 没有注册口，注册机代理池需另行提供）
```

号出来后：

```bash
python3 ~/grok-fullchain/deploy/import_to_grok2api.py \
  --auth-dir ~/grok-stack/grok-register-panel/cpa_auth \
  --also-dir ~/grok-stack/grok-register-panel/grok2api_auth \
  --url http://127.0.0.1:8000 \
  --password-file ~/grok-stack/grok2api/admin-password.txt \
  --assign-nodes <全部使用侧 node id>
```

目录名 `cpa_auth` 只是历史文件名。使用端仍是 Grok2API。

---

## 5. 什么叫装完（你必须自己跑）

- [ ] 解析出的 session 数 = 用户贴的条数（A/B 通用；`from_residential.py` 是 B 路径工具）
- [ ] 方案 B：Mihomo listener 数相同；每个口 `curl --proxy` 有公网 IP。方案 A：proxy-profile 数 = sticky 数；每个节点 test 接口能返回 `exitIp`
- [ ] Grok2API 管理端能登录
- [ ] `/quality-guard` 打得开
- [ ] 使用侧节点数 = 使用侧出口数（A：profile 数；B：listener 数），且 `proxyPool=false`
- [ ] 使用侧 ≥3 时：`failClosed=true`、`softTPS=200`、sidecar 在跑
- [ ] `requestRetry.enabled=true`、`holdTimeout=30s`、`minOutputTokens=8`、`minEncryptedBytes=256`
- [ ] 没有安装 CPA 插件，交付说明里没有 8317
- [ ] 聊天里没有完整代理 URL / admin 密码 / token

缺一条就继续做，不要说「先这样用着」。

---

## 6. 对用户怎么说话

开场问家宽和机器，外加步骤 0 的出口架构二选一；除此之外不要丢 A/B/C 问卷。

收工汇报用这张表：

| 项 | 值 |
| --- | --- |
| Grok2API | URL（常见 `:8000` / `:8181`） |
| 出口架构 | 直连（proxy-profile）或 Mihomo 中转 |
| Guard | `/quality-guard`；mode / failClosed / softTPS |
| 拦截 | requestRetry 开；hold 30s；floor 256B / ×4；burst 开 |
| 家宽 | 用户贴了 N 条 → 使用侧 x 个（A/B 同口径）+ 注册侧 y 个（仅 B） |
| 出口 IP | sticky/节点 → IP（A：节点 test 接口；B：listener 端口）；标出重复 IP |
| 像不像 lab | 使用侧 ≥3 且 Guard 已按本文打开 = 接近；否则写原因 |
| Guard 状态 | passive；还没导账号时探测 idle（noAccountBackoff）属预期，不是故障 |
| 还缺 | 邮箱 / 账号导入 / 重复 IP 的 session |

---

## 7. 不要做

- 不要把官方 `chenyme/grok2api` 当可运行交付：官方 PR 同参数但 **默认不启动拦截**
- 不要把 `RECOMMENDED_DEPLOYMENT` 的「阶段 A：只接一条」当成交付
- 不要问「1024 还是 Kookeey」当分类；问三次 IP 是否相同、yaml 里 `server:` 是什么
- 不要改用户已经在跑的 Grok2API / Mihomo
- 供应商账密：方案 B 只写 Mihomo listener；方案 A 只写在 proxy-profile 里（后端加密存储，reveal 时密码打码）。两条路都不准把账密明文写进聊天、Issue 或交付说明
- 不要在 Issue / 截图 / 聊天回显 `residential.dump`、`mihomo.yaml` 或 proxy-profile 导出里的 username/password
- 不要把 `encrypted_content != ""` 或 `usage.reasoning_tokens` 当成思考证据
- 不要把 holdTimeout 改回 3s（会把 grok-4.6 流末尾密文误判成缺思考）

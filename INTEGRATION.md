# LLM Meter 接入指南

本指南对应当前网页版本，覆盖 pi、omp、Codex、OpenRouter、通用 OpenAI 兼容中转站，以及异地机器的数据汇总。

## 0. 推荐：直接在网页配置站点

启动 `python3 llm_meter.py web`，不需要预先准备 env 文件。在页面下方“数据连接”点击 **配置站点**。

### 通用中转站 / AIHub

填写示例：

| 表单项 | 示例 |
|---|---|
| 站点名称 | AIHub |
| 查询类型 | 通用中转站 · /v1/usage |
| Provider ID | aihub |
| Base URL | https://你的实际中转站域名 |
| 余额路径 | /v1/usage |
| 余额币种 | 跟随接口 / 美元 USD / 人民币 CNY（RMB） |
| 普通 API key | 你的 SK |
| 本地 provider 别名 | my-relay（可选，须与日志中的实际名称一致） |

点击 **保存并查余额**。页面出现该站点的余额、单位、有效状态与采集时间。失败时显示错误；若此前查询成功，保留旧快照并明确标注本次失败，不把旧余额当新结果。

通用查询规则对应你提供的模板：

```text
GET {{站点域名}}{{余额路径}}
Authorization: Bearer {{API key}}

remaining = response.remaining ?? response.quota.remaining ?? response.balance
unit = response.unit ?? response.quota.unit ?? "USD"
isValid = response.is_active ?? response.isValid ?? true
```

按 null / 缺失字段回退，0 余额和 false 状态不会被忽略。支持数值字符串和负余额；没有余额字段或余额不是有限数字时显示错误，不补成 0。币种默认跟随服务商返回的单位；配置时可固定为美元 USD 或人民币 CNY（RMB）。固定币种只覆盖余额单位，不改余额数值、不自动换汇；若与接口单位不同，卡片同时提示原单位。OpenRouter 固定为 USD。此选项只针对余额快照，调用费用和本地估算的 USD 统计口径不变。

Base URL 带 `/v1` 也可以，余额路径按域名根目录拼接，避免 `/v1/v1/usage`。若实际接口为 `/api/v1/usage`，将余额路径改为那个完整站点内路径。支持自定义路径和查询参数，不支持在表单里填写任意 JavaScript extractor；当前直接内置上述解析规则。

### OpenRouter

选择 **OpenRouter · 专用接口**，地址固定为官方站点，填写普通 key 和可选管理 key。

- 普通 key：查询费用摘要及 key 的限额剩余；后者不是账户余额。
- 管理 key：查询账户余额，计算 `total_credits - total_usage`；还可同步模型历史。
- 查询账户余额使用 `/api/v1/credits`，管理 key 权限要求见 [OpenRouter 官方 Credits 文档](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)。
- 已配置站点列表中“同步历史”用于查询该 OpenRouter 站点的历史 token / 模型 / 金额。它按站点 ID 保存独立来源，默认 agent 未知。

多个 OpenRouter key 指向同一账户时，账户余额 / 账户历史仍可能重复覆盖，不能因为是不同站点 ID 就直接相加。页面主“同步 OpenRouter”按钮优先使用列表中的第一个 OpenRouter 站点；没有界面配置才读取环境变量。要同步指定站点，用其列表内的按钮。

### 保存、编辑和删除

保存后立即生效，无需重启。编辑时 key 输入框为空、显示“已保存”掩码；留空保留原 key，填新值替换，勾选清除则删除该凭证。通用站点必须有普通 key，OpenRouter 至少有普通或管理 key。

凭证保存到服务端数据库旁的 `*.connections.json`：例如数据库 `/root/ai/llm-meter/usage.sqlite3` 对应 `/root/ai/llm-meter/usage.connections.json`，权限 600。它是服务端明文凭证文件，不随 CSV 导出；API 不返回原 key，浏览器不使用 localStorage 保存它。可用 `web --connections-file /path/to/private.json` 自定义位置。

演示模式允许保存 / 编辑表单，配置随演示临时目录清理，不查询真实站点。删除站点会删除配置和凭证，已保存的历史用量记录保留。

余额只是独立快照，不能从余额变动推断 agent / 模型的 token 或金额明细。界面保存的凭证目前用于网页查询和历史同步；CLI `proxy` 仍使用自己的 `--key-env` / `--env-file`，不会自动读取网页凭证文件或替 agent 改地址。

## 1. 先弄清楚：在哪里配置什么

网页已支持在“数据连接 → 配置站点”填写和管理站点凭证、查询余额、映射 provider 别名。它不会修改 agent 自己的调用配置。环境变量 / env 文件仍是可选方式。配置分为以下几层：

| 配置对象 | 在哪配置 | 作用 |
|---|---|---|
| LLM Meter 查询 OpenRouter 的凭证 | 网页“配置站点”；也可用终端环境变量或 `meter.env` | 查询费用摘要 / 历史用量 |
| Meter 代理的上游站点和 agent 标签 | `proxy` 启动参数 | 指定请求转发到哪个站点，以及属于哪个 agent |
| pi / omp / Codex 的调用地址和模型 | 各 agent 自己的 provider 配置 | 决定 agent 是否通过 Meter 代理调用 |
| 本地日志 provider 显示名称 | `providers.json`，启动 web 时用 `--provider-map` | 将自定义 provider ID 显示为 AIHub 等名称 |
| 数据库和日志目录 | `--db`、`--pi-home`、`--omp-home`、`--codex-home` | 决定读取 / 保存哪台机器的数据 |

设置 Meter 的 OpenRouter 查询 key **不会**改变 pi / omp / Codex 的模型或上游。反过来，修改 agent 的 provider 配置也不会自动配置 Meter 的远端查询 key。

下面示例假定项目路径为 `/root/ai/llm-meter`。异地机器可放在自己的路径，例如 `/opt/llm-meter`，相应替换命令。项目直接运行只需要 Python 3.10+，不用 Node，也不用安装第三方 Python 运行包。

## 2. 最容易维护的配置方式：私有 env 文件

### 建立配置文件

在运行 Meter 的机器上执行：

```sh
cd /root/ai/llm-meter
mkdir -p "$HOME/.config/llm-meter"
cp config/meter.env.example "$HOME/.config/llm-meter/meter.env"
chmod 600 "$HOME/.config/llm-meter/meter.env"
```

用文本编辑器编辑 `~/.config/llm-meter/meter.env`，内容示例：

```dotenv
OPENROUTER_API_KEY='你的普通OpenRouter推理key'
OPENROUTER_MANAGEMENT_KEY='你的OpenRouter管理key'
AIHUB_API_KEY='你的AIHub推理key'
LOCAL_METER_KEY='你自己选定的本地代理访问token'
```

没有的凭证保留为空，例如 `OPENROUTER_MANAGEMENT_KEY=`。这些都是占位符，必须替换成真实值才能访问服务商。`LOCAL_METER_KEY` 是你为本地代理指定的独立 token，不是 AIHub 或 OpenRouter 的 key。

### 加载配置并启动

```sh
python3 llm_meter.py \
  --env-file "$HOME/.config/llm-meter/meter.env" \
  --db /root/ai/llm-meter/usage.sqlite3 \
  web --port 8765
```

浏览器打开 **http://127.0.0.1:8765**。

`--env-file` 和 `--db` 都必须放在子命令前。文件不自动加载，所有需要这些凭证的 `web`、`sync`、`proxy` 命令都要传 `--env-file`，或者使用已经设置好的进程环境变量。

配置文件规则：

- 一行一个 `NAME=value`；允许空行和以 `#` 开头的注释。
- 值含空格时用引号括起来；需要保留 `#` 时也应加引号。
- 支持可选的 `export NAME=value`，但不会执行任何 shell 代码。
- 不展开 `$HOME`、`${VARIABLE}`、命令替换等，文件里的值按字面读取。
- 显式配置文件覆盖启动进程中的同名环境变量；空值也会覆盖。
- 编辑文件后重启网页或代理，现有进程不会自动重载。
- 用 `--pi-home` 等参数配置日志位置，不在凭证文件中设置它们。

凭证不会返回给浏览器或写进调用数据库。项目忽略 `*.env`，但建议仍将真实配置放在上述私有目录。

### 不用文件也可以

在**启动服务的同一个终端**中设置环境变量：

```sh
export OPENROUTER_API_KEY='你的普通key'
export OPENROUTER_MANAGEMENT_KEY='你的管理key'
python3 llm_meter.py web
```

另一个终端后来设置的变量，不会影响已启动的网页进程。若环境里有变量、又传了 env 文件，以文件为准。

## 3. OpenRouter：查询历史与采集推理是两件事

### A. 只看当前普通 key 的费用摘要

配置 `OPENROUTER_API_KEY`，启动网页后点击下方 **“同步 OpenRouter”**。

成功后，数据连接区域显示今日 / 本周 / 本月 / 累计美元费用与 key 限额信息。这个摘要不是逐模型事件，所以不会单独生成 agent / 模型占比，也不能算每百万 Token 费用。

CLI 等价操作：

```sh
python3 llm_meter.py \
  --env-file "$HOME/.config/llm-meter/meter.env" \
  --db /root/ai/llm-meter/usage.sqlite3 \
  sync --openrouter
```

普通 key 信息接口：`GET https://openrouter.ai/api/v1/key`。其费用与限额字段以 [OpenRouter 当前 key 文档](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key) 为依据。

### B. 看模型、Token、调用量和历史金额

再配置 `OPENROUTER_MANAGEMENT_KEY`，重启网页，再点同步。

网页选择：

1. 采集数据集：**远端历史**。
2. 记录来源：**openrouter:activity:account**。
3. 日期：最近 30 天，或这段范围内的自定义日期。
4. Agent：全部或未知；模型按需选择。
5. 趋势指标：金额；费用类型：服务商费用。

Activity 接口需要管理 key，覆盖最近 30 个已结束的 UTC 日，不包含尚未结束的 UTC 日。管理 key 默认查询账户范围，而不是只查询 `OPENROUTER_API_KEY` 对应的 key。[OpenRouter Activity 文档](https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity-grouped-by-endpoint)

网页自动刷新本地数据，不会周期性查询远端账单。需要更新 OpenRouter 时再次点击同步。页面“已配置”只代表变量非空，实际权限是否有效要以同步结果为准。

### C. OpenRouter 历史怎么区分 pi / omp / codex

服务商历史通常不知道调用它的 agent。如果多个 agent 共用同一 key，不应强行归属。

建议为每个 agent 创建独立普通推理 key。在你取得该 key 的管理接口 hash 后：

```sh
python3 llm_meter.py \
  --env-file "$HOME/.config/llm-meter/meter.env" \
  --db /root/ai/llm-meter/usage.sqlite3 \
  sync --openrouter --key-hash '实际pi专用key的hash' --agent pi
```

网页“记录来源”选择 `openrouter:activity:实际hash`，就能看到 agent=pi 的历史。

`hash` 不是原始 API key，也不是随便填写的名称。`--agent` 必须与 `--key-hash` 一起使用；只有确定该 key 专供这个 agent 时才这样标记。

账户历史和单 key 历史可能覆盖同一调用，务必选择一个具体记录来源统计，不要一起相加。多账户费用摘要只有一个固定 `openrouter:key` 快照，若要隔离账户请使用独立数据库。

### D. 采集现在的 OpenRouter 调用

使用**普通推理 key**启动专门给 pi 的代理：

```sh
python3 llm_meter.py \
  --env-file "$HOME/.config/llm-meter/meter.env" \
  --db /root/ai/llm-meter/usage.sqlite3 \
  proxy --upstream https://openrouter.ai/api/v1 \
  --key-env OPENROUTER_API_KEY \
  --local-key-env LOCAL_METER_KEY \
  --name pi-openrouter --agent pi --provider openrouter --port 8787
```

pi 的 OpenAI 兼容 provider 配置改为：

| 项目 | 填什么 |
|---|---|
| API base URL | `http://127.0.0.1:8787/v1` |
| 客户端 API key | `LOCAL_METER_KEY` 的实际值 |
| 模型 | OpenRouter 支持的真实模型 ID |

网页选择 **代理采集 → agent=pi → provider=openrouter**。代理使用普通 key 推理，不能使用管理 key。这里的模型费用只有上游返回 `usage.cost` 时才已知。

## 4. AIHub 和其他中转站：完整接入流程

目前支持 OpenAI 兼容推理代理，尚未适配某个具体 AIHub 的历史查询接口。AIHub 有同名服务，需要先确认你的实际官网、API base URL、账单接口文档。

### 步骤 1：取得正确的 base URL

应填服务商提供的 API 前缀，例如：

```text
https://你的站点/v1
```

不是官网首页，也不是完整的 `/chat/completions` 地址。OpenRouter 的 API 前缀则是 `https://openrouter.ai/api/v1`。

### 步骤 2：Meter 配置上游 key

将 key 写进私有 env 文件中的 `AIHUB_API_KEY`，或在代理启动终端中 export。上游地址、agent 和 provider 名称仍由启动参数指定。

### 步骤 3：每个 agent 分配专用代理

给 pi：

```sh
python3 llm_meter.py \
  --env-file "$HOME/.config/llm-meter/meter.env" \
  --db /root/ai/llm-meter/usage.sqlite3 \
  proxy --upstream https://YOUR-AIHUB-HOST/v1 \
  --key-env AIHUB_API_KEY --local-key-env LOCAL_METER_KEY \
  --name pi-aihub --agent pi --provider aihub --port 8787
```

给 omp，在另一个终端运行同样的命令，将后三项改成：

```text
--name omp-aihub --agent omp --provider aihub --port 8788
```

给 Codex，再启动一个实例，将后三项改成：

```text
--name codex-aihub --agent codex --provider aihub --port 8789
```

### 步骤 4：配置 agent 自己的 provider

| Agent | base URL | API key |
|---|---|---|
| pi | http://127.0.0.1:8787/v1 | LOCAL_METER_KEY 的值 |
| omp | http://127.0.0.1:8788/v1 | LOCAL_METER_KEY 的值 |
| Codex 自定义兼容 provider | http://127.0.0.1:8789/v1 | LOCAL_METER_KEY 的值 |

“改在哪个文件、配置字段叫什么”由你的 agent 版本及 provider 扩展决定。Meter 不会修改它们的配置，也不能把 ChatGPT OAuth provider 自动改造成普通 API provider。

如果没启用 `--local-key-env`，本机代理接受占位 key；上游 key 由 Meter 替换。启用时则必须使用正确的本地 token。agent 配置的真实上游模型 ID不变。

### 步骤 5：验证

先完成一次真实推理调用，再在网页选“代理采集”，检查：

- agent 为你指定的 pi / omp / codex；provider 为 aihub。
- 记录来源为 `proxy:pi-aihub` 等。
- 模型为上游实际返回的模型或请求模型。
- 输入 / 输出 Token 有值；若没有，查看“缺失 usage”提示。
- 金额可能是未知：OpenAI 兼容并不意味着响应会返回费用。

代理在调用结束后保存记录，不在每个生成 token 到达时更新统计。它支持 Chat Completions、Completions、HTTP Responses 和 Embeddings 的 usage，不支持 WebSocket、原生 Anthropic `/messages` 用量采集或 multipart 上传。

流式 Chat 请求默认加 `stream_options.include_usage=true`，上游不支持时加 `--no-include-usage`。这能保持兼容，但不保证还能得到 token。

### 不想改 agent 调用方式怎么办

网页默认已读取 pi / omp / Codex 本地会话日志，通常能取得 token 与 provider 名称。pi/omp 的金额是本地估算，不自动当作中转站账单。

若你的 AIHub provider 在日志中叫 `my-relay`，建立 `providers.json`：

```json
{"my-relay":"aihub"}
```

启动时加 `web --provider-map /path/to/providers.json`。这只改显示和筛选，不查询 AIHub，也不改变金额来源；确认别名指向同一站点后再映射。

## 5. openai 与 openai-codex 为什么分开

这里要区分 **agent 程序**、**provider ID / 接入方式** 和 **模型 ID**。

| 看到的内容 | 在哪出现 | 含义 |
|---|---|---|
| agent=codex | Agent 筛选 | 调用来自 Codex 的本地会话日志，或被指定为 codex 的代理 |
| provider=openai | Provider 筛选 | 日志记录的 provider ID；在 pi 的内置 provider 中通常是普通 OpenAI API |
| provider=openai-codex | pi/omp Provider 筛选 | 这类客户端中用于 ChatGPT/Codex OAuth 接入的 provider，与普通 API provider 分开 |
| model 中含 codex | 模型筛选 | 模型名称；单凭模型名无法判断调用它的是哪个 agent 或付费方式 |

本机安装的 pi 实现中，`openai-codex` OAuth 模块明确标注为 ChatGPT OAuth；这是对该客户端 provider 的核对，不能据此推广到所有自定义 provider 名称。自定义 provider 可以任意命名，真实含义仍以你配置为准。

OpenAI 官方说明 Codex 支持 ChatGPT 登录和 API key 登录；API key 用量按 OpenAI Platform API 方式计费。它们不是同一个付费入口。[OpenAI 官方认证文档](https://learn.chatgpt.com/docs/auth)

特别注意：**Codex 自己的日志里 `model_provider=openai`，不代表它一定使用 API key 按 token 付费**；也可能是 ChatGPT 登录。当前 Meter 没有从日志中识别 Codex 的认证模式，不能凭 provider=openai 给它补算实际金额。

因此：

- 不要为了图表整齐，把 openai 与 openai-codex 强行映射为同一项。
- 同一 OpenAI 模型可能被 pi、omp、Codex 调用，筛 agent 和筛 model 是两件事。
- pi/omp 的 `usage.cost.total` 保存为估算费用；OAuth 订阅调用也可能报告 API 等价金额，它不是你的订阅账单。
- 若要比较实际付款，需要服务商费用记录；若要比较不同 agent 的消耗，可参考本地估算，但要看覆盖范围。

## 6. Token 费用比在哪里，如何计算

已增加网页 **“Token 费用比”** 区域，位于顶部四张摘要卡片下方，显示两个独立指标：

- 服务商费用 / 百万 Token。
- 本地估算费用 / 百万 Token。

计算公式：

```text
单位费用 USD / 1M Token
= 同一批有费用且有正数 Token 的记录费用合计
  ÷ 这些记录的（输入 Token + 输出 Token）合计
  × 1,000,000
```

例如，一批记录有 2,000,000 Token、已知费用 $4，则服务商单位费用为 **$2 / 1M Token**。如果另有 8,000,000 Token 费用未知，它们不会加入这个指标的分母；否则会错误显示为 $0.4 / 1M。

覆盖 Token 数和记录条数显示在指标下方。缺失 usage 的记录、零 token 记录不进入该比值；没有可用记录时显示未知，不显示假单价。总费用卡片仍展示全部已知费用，因此它的覆盖范围可能与单价不同。

这是实际用量组合的**有效混合单价**，不是服务商公开价目表。不同输入/输出比例、缓存占比、模型分布会影响它。缓存、缓存写入、推理属于子集，不重复加到分母。

分组明细表还有“费用 / 1M Token”列：默认服务商费用口径；先将趋势指标切为金额，再选本地估算，可以切换该列的金额口径。两张单位费用卡片始终同时保留两种口径。

如果你说的“比”是 **费用占比**：趋势指标选“金额”，费用口径选服务商或估算，下方环形图切换 Agent / 模型，就会显示各组费用百分比。若看 Token 占比，切回总 Token。百分比构成和单位 Token 成本是两个独立概念。

## 7. 异地接入：选一种方式，不要重复汇总

| 需求 | 推荐方式 | 是否在异地部署 Meter |
|---|---|---|
| 只想查看异地机器自己的页面 | 异地运行 web，SSH 隧道访问 | 是，运行网页 |
| 想把多台机器的本地 agent 历史汇总到一个页面 | 每台机器采集并导出 JSONL，中心机器按前缀导入 | 是，异地只需 CLI，不必运行网页 |
| 新调用都想在一个中心即时统计 | 中心运行代理，异地 agent 通过 SSH 隧道接入 | 异地不需要 Meter，需配置 agent 和 SSH |

当前没有自动发现设备、联网 collector push API 或在线远端拉取器。下面使用已经实现的日志导出导入和代理机制；SSH / scp 示例需要你自己的主机账号，本文不会自动登录或执行。

### 方案 A：异地独立网页

在 agent 所在机器部署项目代码，使用该账号能读取的日志目录：

```sh
cd /opt/llm-meter
python3 llm_meter.py --db /opt/llm-meter/usage.sqlite3 web --port 8765
```

在你自己的电脑：

```sh
ssh -N -L 18765:127.0.0.1:8765 your-user@remote-host
```

本机浏览器打开 **http://127.0.0.1:18765**。这只是查看远端网页，不会把远端事件复制到本机数据库。

Meter 仅绑定本机地址，不能通过 `web --host 0.0.0.0` 直接暴露网页。远端文件路径、日志账号和 CODEX_HOME 要按实际情况填写。

### 方案 B：多台机器历史汇总到中心页面

#### 第一步：在异地机器同步日志

```sh
cd /opt/llm-meter
python3 llm_meter.py --db /opt/llm-meter/collector.sqlite3 sync
```

如日志不在默认位置：

```sh
python3 llm_meter.py --db /opt/llm-meter/collector.sqlite3 sync \
  --pi-home /home/your-user/.pi/agent \
  --omp-home /home/your-user/.omp/agent \
  --codex-home /home/your-user/.codex
```

#### 第二步：只导出本地日志数据

```sh
python3 llm_meter.py --db /opt/llm-meter/collector.sqlite3 \
  export --dataset local --days 30 > /tmp/server-a-usage.jsonl
```

`--dataset local` 避免把同一调用的代理和服务商历史也一起导出。JSONL 不含 prompt、回复正文或 key，保留 agent、provider、model、Token、费用和估算。

#### 第三步：将文件传到中心机器

在中心机器执行，假定中心项目路径为 `/root/ai/llm-meter`：

```sh
cd /root/ai/llm-meter
mkdir -p incoming
scp your-user@remote-host:/tmp/server-a-usage.jsonl ./incoming/server-a-usage.jsonl
```

#### 第四步：按机器前缀导入

```sh
python3 llm_meter.py --db /root/ai/llm-meter/central.sqlite3 \
  import ./incoming/server-a-usage.jsonl --source-prefix server-a
```

原来源 `pi:openrouter` 会变成 `import:server-a:pi:openrouter`，原来源 `codex:openai` 会变成 `import:server-a:codex:openai`。agent/provider/model 不丢失，所有导入事件属于 import 数据集。

第二台机器使用另一个稳定前缀：

```sh
python3 llm_meter.py --db /root/ai/llm-meter/central.sqlite3 \
  import ./incoming/laptop-b-usage.jsonl --source-prefix laptop-b
```

不要将不同机器都命名为 server-a。相同机器重复导出、导入同一条稳定 id 会更新，不会累加；不同原来源的相同 id 也由来源隔离。坏行会让该次导入整体回滚。

#### 第五步：启动中心网页

```sh
python3 llm_meter.py --db /root/ai/llm-meter/central.sqlite3 web --no-sync
```

网页选择 **导入记录**，然后自由筛 agent / model / provider；记录来源可精确选某台机器的来源。`--no-sync` 防止中心机器本地日志混入这个专用于异地汇总的库。

#### 定期更新

重复执行“异地 sync → export → scp → 中心 import”。可用你自己的 cron / systemd timer 自动调度；当前工具不自动建立任务。始终使用同一机器前缀、同一中心数据库和稳定 ID。

滚动导出 30 天只是本次文件窗口，不会删除中心库更早的记录。若异地删除记录，中心不会自动镜像删除；导入只做更新 / 添加，不做删除同步。页面仍需选择合理日期范围。

不要把多台机器的运行中 SQLite 文件放到网络共享盘让它们共同写。各自采集、传 JSONL、中心导入即可。

### 方案 C：异地 agent 通过中心代理采集新调用

在中心机器启动专属于异地 pi 的代理：

```sh
python3 llm_meter.py \
  --env-file "$HOME/.config/llm-meter/meter.env" \
  --db /root/ai/llm-meter/central-proxy.sqlite3 \
  proxy --upstream https://openrouter.ai/api/v1 \
  --key-env OPENROUTER_API_KEY --local-key-env LOCAL_METER_KEY \
  --agent pi --provider openrouter --name server-a-pi-openrouter --port 8787
```

在异地 agent 机器建立隧道：

```sh
ssh -N -L 18787:127.0.0.1:8787 central-user@central-host
```

异地 pi 的兼容 provider 填：

```text
base URL = http://127.0.0.1:18787/v1
API key  = 中心代理 LOCAL_METER_KEY 的实际值
```

中心网页使用同一个 `central-proxy.sqlite3`，选择“代理采集”。这些新调用直接写入中心库，不需要在异地运行 Meter。

隧道必须保持连接；断开后 agent 请求会失败。不同 agent / 机器想分别标记时，分配不同中心代理实例和名称。该方案不能恢复过去的调用历史，也不适用于原生 OAuth ChatGPT provider 的直接替换。

不要再把覆盖同一调用的异地本地日志导入并一起相加。可保留两套数据用于核对，但分别选数据集和来源查看。

## 8. 首次接入验收清单

1. 用 `web --demo --port 8766` 能看到趋势和占比，确认页面可用。
2. 用实际数据库 `web` 看到 pi / omp / codex 日志；如果为空，检查日志根目录。
3. OpenRouter 点击同步成功；普通 key 只有摘要，管理 key 才有远端模型历史。
4. 代理完成一次兼容推理，Agent 与 Provider 标签准确；客户端实际访问的是代理地址。
5. 金额未知时不误当免费，pi/omp 本地估算不误当付款金额。
6. 单位费用显示覆盖 Token 和记录数，不使用所有 Token 与部分费用混算。
7. 异地重复导入同一文件后记录数不增长；另一个机器前缀不会覆盖原机器。
8. 选一个采集数据集 / 具体来源看合计，避免日志、代理和远端历史重复。

## 9. 还需要提供哪些信息，才能完成你的具体接入

不需要把真实密钥发到聊天中。你可以提供：

- AIHub 实际官网、API base URL、用量或账单 API 文档链接。
- pi、omp、Codex 的版本；使用默认 provider 还是自定义 provider。
- 想在哪台机器放中心页面、哪些机器运行 agent，以及这些机器的系统类型。
- 隐去 key 的 provider 配置片段，尤其是 provider ID、base URL、API 类型和模型名。
- 是否已有 OpenRouter 管理 key，以及是否能给各 agent 分配独立推理 key。

有这些信息后，可以进一步给出与你实际版本匹配的 agent 配置，而不把某一种客户端的配置格式当成通用格式。


## 编辑已保存站点、DeepSeek 与 Codex 实时额度

保存配置后可随时修改：在「数据连接」的站点余额卡片点击 **编辑配置**，或点击 **管理站点 → 编辑**。修改名称、地址、余额路径、Provider、别名、币种或密钥后点击 **保存修改**；立即生效，无需重启。密钥留空保留原值，输入新密钥替换；不会回显已保存密钥。OpenRouter 官方地址和 USD 固定，其他字段可编辑。

### DeepSeek 官方账户

1. 「管理站点 → 新增」，查询类型选择 **DeepSeek · 官方余额接口**。
2. 名称填 DeepSeek，Provider ID 填 `deepseek`；自动填入 Base URL `https://api.deepseek.com` 和余额路径 `/user/balance`。
3. 填写 DeepSeek API key，币种建议 **跟随接口**，点击 **保存并查余额**。
4. 卡片显示可用余额、赠金和充值余额，支持接口返回的 CNY / USD；选择固定币种会选择对应币种账户，缺失时提示错误，不换汇或重标金额。

余额协议见 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/)。余额接口不能提供完整调用历史；agent、模型、token 用量仍通过 pi/omp 本地日志、专用 OpenAI 兼容代理或数据导入接入。代理 upstream 可设置为 `https://api.deepseek.com`，Provider 设置 `deepseek`，agent 填实际调用客户端。

### Codex 随时查看剩余额度

「数据连接 → Codex 剩余额度 / credits → 查询实时额度」调用服务器上 `codex app-server` 的只读 `account/rateLimits/read`，显示各窗口剩余比例、重置时间、查询时间及接口提供的 credits。不会启动推理任务。普通页面自动刷新读取已保存快照；需要新的账户额度时再次点击查询。日志快照与实时查询均明确标注来源和时间，失败保留上次快照。

先在运行此工具的机器安装 Codex CLI，并用 ChatGPT 账户执行 `codex login`。自定义登录目录可用：

```sh
python3 llm_meter.py web --codex-home /path/to/codex-home
```

额度不是美元账户余额。仅 API key / 中转站登录不保证支持此额度接口，请在站点卡片查询供应商金额余额。credits 按接口原值显示，不推断币种。服务商未返回 credits 时不会虚构金额。部署在另一台机器时，额度属于该机器的 Codex 登录账号；导入用量 JSONL 不会同步登录状态或实时额度。不需要在网页粘贴 Codex 登录 token。

接口说明见 [OpenAI 官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)。演示模式不连接真实服务。

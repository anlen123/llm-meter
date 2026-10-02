# 配置示例

也可直接在网页“数据连接 → 配置站点”配置凭证并查询余额，无需 env 文件；本页保留文件方式，CLI 代理仍使用 env。

这两个示例文件分别用于凭证和 provider 名称映射。这里没有真实密钥；`REPLACE_WITH_...` 必须替换，不使用的凭证留空。

## 1. 复制后编辑

在项目目录执行：

```sh
cd /root/ai/llm-meter
cp config/meter.env.example config/meter.env
chmod 600 config/meter.env
cp config/providers.json.example config/providers.json
```

编辑 `config/meter.env`：

- `OPENROUTER_API_KEY`：普通 OpenRouter key，用于费用摘要 / 代理推理。
- `OPENROUTER_MANAGEMENT_KEY`：可选管理 key，用于历史模型、token、金额查询。
- `AIHUB_API_KEY`：AIHub 推理 key。
- `RELAY_B_API_KEY`：可选第二个中转站 key。
- `LOCAL_METER_KEY`：自己设定的代理访问 token，客户端用它访问本地代理。

生成随机本地 token 后，把输出手动填入 `LOCAL_METER_KEY`：

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

编辑 `config/providers.json`：左边填你本地日志中的真实 provider ID，右边填网页上希望显示的站点名。例子里的 `my-aihub-provider` 等不是自动识别的 ID，需要替换；不用映射时可改成 `{}`，或者启动网页时不传 `--provider-map`。不要把 `openai` 与 `openai-codex` 随意映射为同一项。

## 2. 启动网页

```sh
python3 llm_meter.py \
  --env-file config/meter.env \
  --db /root/ai/llm-meter/usage.sqlite3 \
  web --port 8765 --provider-map config/providers.json
```

打开 http://127.0.0.1:8765。点击页面下方“同步 OpenRouter”查询费用。普通 key 只有费用摘要，管理 key 才能查询远端模型历史；相应事件在“远端历史”数据集中。

网页会自动读取本机 pi / omp / Codex 日志。`--env-file` 仅加载凭证，不会修改 agent 的 provider 配置。编辑文件后需要停止并重新启动服务。

## 3. pi 通过 AIHub 代理采集

在另一个终端执行；把上游地址占位符换成你实际的 API base URL：

```sh
cd /root/ai/llm-meter
python3 llm_meter.py \
  --env-file config/meter.env \
  --db /root/ai/llm-meter/usage.sqlite3 \
  proxy --upstream https://YOUR-AIHUB-HOST/v1 \
  --key-env AIHUB_API_KEY --local-key-env LOCAL_METER_KEY \
  --name pi-aihub --agent pi --provider aihub --port 8787
```

pi 的兼容 provider 配置填：

```text
base URL： http://127.0.0.1:8787/v1
API key：  config/meter.env 里 LOCAL_METER_KEY 的实际值
模型：     AIHub 支持的实际模型 ID
```

网页选择“代理采集”，Agent 选 pi，中转站选 aihub。完成一次调用后才会出现记录；上游没有返回费用时，金额仍为未知。

给 omp / Codex 使用时另启代理实例，分别修改 `--agent`、`--name` 和 `--port`，并让对应客户端指向专用端口。不要多个 agent 混用同一个带固定 agent 标签的代理。

## 4. pi 通过 OpenRouter 代理采集

```sh
python3 llm_meter.py \
  --env-file config/meter.env \
  --db /root/ai/llm-meter/usage.sqlite3 \
  proxy --upstream https://openrouter.ai/api/v1 \
  --key-env OPENROUTER_API_KEY --local-key-env LOCAL_METER_KEY \
  --name pi-openrouter --agent pi --provider openrouter --port 8788
```

客户端 base URL 改为 http://127.0.0.1:8788/v1，API key 使用同一个 `LOCAL_METER_KEY` 的值；上游使用普通推理 key，不能使用管理 key。

## 5. 第二个中转站

先填写 `RELAY_B_API_KEY`，然后执行：

```sh
python3 llm_meter.py \
  --env-file config/meter.env \
  --db /root/ai/llm-meter/usage.sqlite3 \
  proxy --upstream https://YOUR-SECOND-RELAY/v1 \
  --key-env RELAY_B_API_KEY --local-key-env LOCAL_METER_KEY \
  --name omp-relay-b --agent omp --provider relay-b --port 8789
```

对应客户端地址为 http://127.0.0.1:8789/v1。网页和所有代理使用同一个绝对数据库路径。

异地汇总、provider 差异和 token 单位费用的说明见 [完整接入指南](../INTEGRATION.md)。

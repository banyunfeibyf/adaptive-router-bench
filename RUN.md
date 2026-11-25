# 运行自适应路由器

`router.py` 使用 Python 3.9 或更新版本的标准库，通过 OpenAI 兼容的 Chat Completions 接口发起真实调用。平台须支持同步文本回复。执行时模型可看到输入及执行所需前文，并可能产生费用。

## 配置与运行

先将三个示例名称改成自己的模型标识，密钥从运行环境注入。

```sh
export ROUTER_BASE_URL='https://api.openai.com/v1'
export ROUTER_LOW_MODEL='your-low-model'
export ROUTER_MID_MODEL='your-mid-model'
export ROUTER_HIGH_MODEL='your-high-model'
# 从环境或密钥管理器提供 ROUTER_API_KEY

state="$HOME/.adaptive-router/demo.sqlite"
python3 router.py --state "$state" route --family 'exhibit-labels' --json \
  '写一条虚构的博物馆展览预告，限 60 字。'
```

共用接口在 `ROUTER_BASE_URL` 中配置，可写 API 基础地址或完整的 `/chat/completions` 地址。共用密钥使用 `ROUTER_API_KEY`。不同服务可分别设置 `ROUTER_LOW_BASE_URL` 和 `ROUTER_LOW_API_KEY`，中、高档替换变量中的 `LOW`。档位改用不同接口时，使用对应档位的密钥变量。无鉴权的开发接口可使用空密钥。

每次先调用低档判定。简单题可由该次调用直接回答。其他题默认只交给一个执行模型。显式加 `--staged` 后，可按计划拆分为检索现有材料、核验、判断、成稿等阶段。前序阶段产出会随原问题交给后续阶段。工具不执行外部检索，检索阶段只处理输入已有材料。

- `--input FILE` 从 UTF-8 文件读问题。未给位置参数或文件时，可从标准输入读取。
- `--tier low|mid|high` 手动指定执行档位并跳过记忆，判定调用仍使用低档。
- `--risk critical` 将执行档位默认提为高档，关闭该题探索。手动档位优先。
- `--no-memory` 跳过本地记忆和探索。省略 `--family` 时，分类器从六个常用类别中选择，报告会列出本轮类别。长期重复的同类任务可用 `--family` 固定类别。反馈按类别、执行阶段和模型绑定隔离。
- `--json` 在标准输出返回答复和调用报告。省略时仅答复进入标准输出，报告写入标准错误。报告按调用给出提供方返回的已知 token 数，包含分类调用。未返回用量的调用仍列出。阶段报告包括初判后的基准档、选择方式、理由和实际请求的模型；接口返回模型标识时也会记录。

## 反馈与探索

对报告中的 `decision_id` 提交反馈。分阶段运行时，先判断问题来自哪个已完成阶段，使用对应 ID。如果工具、本地配置或证据缺失导致问题，可选择 `external` 或 `unclear`，这样不会影响模型档位。

```sh
python3 router.py --state "$state" feedback --id 'REPLACE_WITH_DECISION_ID' \
  --signal corrected --attribution model --reason '成稿遗漏了已明确给出的字数限制'
python3 router.py --state "$state" explore on
python3 router.py --state "$state" explore off
```

探索默认关闭。开启后，普通风险任务以约一成的决定 ID 抽样试用。可提供 `ROUTER_CHALLENGER_MODEL` 及可选的 `ROUTER_CHALLENGER_BASE_URL`、`ROUTER_CHALLENGER_API_KEY`，使已可调用的新候选参与探索。未提供候选时，当前基准模型近期三个有效反馈均满意才可能试低一档。

同一任务类别、阶段、基准档位和模型绑定内，替代模型最近三个有效试用反馈均为明确满意时，后续普通任务默认采用该模型。试用与默认执行分别记录，只有试用收到的明确模型归因满意计入采用门槛。采用后收到一次明确的模型归因纠正即回到基准策略。至少完成三次基准执行并在新的试用中重新积累三次满意，才会再次采用。关键风险及手动指定的档位不参与。

`explore off` 后停止试用和采用，恢复基准策略。基准模型自身连续两次被明确纠正时，常规升档规则仍有效。升档后的模型近期三个有效反馈均满意时，开启探索的普通任务可从这个档位开始新的试用。重新开启探索可以继续使用已有的有效证据。任意一轮可用 `--no-memory` 跳过全部本地反馈决策。

SQLite 默认位于 `~/.adaptive-router/state.sqlite`，可用全局参数 `--state` 指定项目自己的数据库。数据库保存任务类别、档位决定和显式反馈。问题、答复和密钥不写入数据库。JSON 报告包含原答，分享报告前请核对内容。

## 离线检查

在仓库目录执行 `python3 -B -m unittest -v test_router`。测试使用模拟 HTTP 响应及临时 SQLite，不需要 API 密钥或联网。

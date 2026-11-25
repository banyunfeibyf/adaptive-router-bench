# 命令行使用

`bench.py` 使用 Python 标准库。所有命令在项目目录运行。默认题库是同目录的 `questions.json`，默认结果目录是运行目录下的 `wb-results`。以下示例用临时目录保存结果。

## 初始化与读取题目

```sh
scratch="$(mktemp -d)"
run="$scratch/run"

python3 bench.py init --out "$run"
python3 bench.py next --out "$run" --arm mid
python3 bench.py meta --arm mid > "$scratch/meta.json"
```

默认策略为 `low`、`mid`、`high` 和 `route`。初始化时加 `--native-auto` 可加入平台原生自动模式。请在 `run.json` 的 `models` 中填写实际模型标识与绑定证据。路由分类器 `router` 和轻量模型 `low` 使用一致的模型标识。

新题库的顶层是包含 `topics` 数组的 JSON 对象。每个主题需要唯一的 ASCII 字母数字 `id`、非空 `title` 和包含三个非空字符串的 `questions`。用 `--questions PATH` 初始化自定义题库时，在此后的 `next`、`record` 和 `pack` 中使用同一题库。工具按初始化时的题库散列值检查一致性。

## 记录答复

把模型输出原文保存为 `$scratch/answer.txt`。根据 `meta` 生成的模板编辑 `$scratch/meta.json`，填写来源可复核的实际调用。下面的命令对应示例题库的第一题。

```sh
python3 bench.py record --out "$run" --arm mid --id museum.1 \
  --file "$scratch/answer.txt" --meta "$scratch/meta.json"
python3 bench.py next --out "$run" --arm mid
```

- `session_ref`、`call_log_complete` 和 `fallback` 记录会话证据、调用清单是否完整及是否发生回退。按实际证据填值，未知值可留 `null`。
- 每次调用填写 `kind`、`outcome`、`model_id` 和 `evidence`。分类、失败或被舍弃的调用也进入成本记录。低档一次调用完成分类和回答时，使用一个 `classify_and_answer` 条目。路由策略另填 `route.level` 与分级证据。
- `actual` 使用 `amount`、`unit`、`channel` 和 `evidence`。估算使用 `estimate` 下的 `amount`、`unit`、`channel` 和 `method`。无法确认的金额保留 `null`。
- 实验者有独占策略区间的账单时，可以在 `run.json` 的 `arm_bills` 中填写账单类型、起止金额、单位、渠道、证据、是否独占、覆盖首尾题号和记录数。账单类型可为累计支出 `cumulative_spend` 或剩余额度 `remaining_balance`。账单的首尾题号按题库的出题顺序填写。
- 必须在各策略内按题库顺序写入。彻底失败时加 `--failed`，让 `--file` 指向一份含失败原因的文本。失败会占用该回合并记入完整性报告。

## 导出与评分

```sh
# 小规模试跑或只运行了部分策略
python3 bench.py pack --out "$run" --partial

# 填齐本次启用策略的全部记录后
python3 bench.py pack --out "$run"
```

导出目录和 ZIP 归档均位于 `$run/exports/`。其中 `blind/answers.jsonl` 以随机会话编号保留原答全文。`private/` 含策略映射、原始记录、运行配置、成本与覆盖率。评审前从 ZIP 单独提取匿名评审文件，并核查题目和原答中是否有模型自述或敏感内容。评分结束后再用映射还原策略。归档和 SQLite 数据库应保留在本地。

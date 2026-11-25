# 分级路由

每个回合用当轮问题及必要前文做一次复杂度判定，随后调用指定档位。开始实验前，记录你实际可用的三档模型标识，同一轮对照期间使用一致的绑定。

| 档位 | 选择依据 | 执行方式 |
| --- | --- | --- |
| `low` | 输入完整，操作少，结果容易检查。 | 轻量模型可以在同一次调用中先判档再作答。 |
| `mid` | 目标明确，需要多步计算、整理、推理或保留多轮约束。 | 轻量模型完成初判，中档模型用完整题目和必要前文作答。 |
| `high` | 有会改变结论的关键未知，涉及高风险判断，或需要合并多条依赖并复核。 | 轻量模型完成初判，高档模型处理问题、核验决定性前提并标出无法核实的部分。 |

可把下面的指令交给轻量模型，并由自己的运行环境传入当前回合的题目和前文。

> 根据当前问题和必要前文判断最小可胜任档位。输出 `low`、`mid` 或 `high`，附一条具体理由。判为 `low` 时同时给出最终答复。判为 `mid` 或 `high` 时，把完整问题、前文中的有效约束和判档理由交给对应档位。

若分类和生成是两个调用，逐个记录。模型返回新证据并促使升档时，记录原调用的结果、后续调用和变动缘由。按 [CLI.md](CLI.md) 的格式保存判档依据和实际模型标识，让成本统计覆盖全链路。

## 可选的本地反馈记忆

`router_memory.py` 接受已得出的初判，结合相同任务类别、阶段和模型的历史反馈提出模型选择。它使用 SQLite 保存决定及反馈。对照实验宜采用固定规则，把长期反馈试验安排在另一个运行中。

```sh
scratch="$(mktemp -d)"
state="$scratch/router.sqlite"
printf '%s\n' '{"low":"demo-low","mid":"demo-mid","high":"demo-high"}' > "$scratch/models.json"

python3 router_memory.py --state "$state" choose \
  --id exhibit-demo-1 --family exhibit-demo --stage compose \
  --initial low --models "$scratch/models.json"

python3 router_memory.py --state "$state" feedback \
  --id exhibit-demo-1 --signal satisfied --attribution model \
  --reason '虚构展签保留了指定字段'
```

示例中的模型标识是占位符，实际调用时填写平台可用的真实模型标识。可选阶段为 `retrieve`、`verify`、`judge`、`compose`。反馈信号可为 `satisfied`、`corrected`、`followup` 或 `unclear`，并标明归因。同一决定 ID 可以用同样参数重读，改用不同参数时应使用新 ID。

同类同阶段的同一模型最近两次有效反馈均为明确纠正时，工具会上调一档（已在高档时保持高档）。探索默认关闭，可用以下命令切换。

```sh
python3 router_memory.py --state "$state" explore on
python3 router_memory.py --state "$state" explore off
```

开启后，只有普通风险任务可尝试探索，由决定 ID 的稳定散列抽样。可通过 `--challenger` 指定可调用的新候选。没有候选时，当前模型近期三次有效反馈均为明确满意，才可能尝试降低一档。存在连续纠正记录的候选会被跳过。决策输出用于本地编排，实际模型调用由实验者的运行环境执行。

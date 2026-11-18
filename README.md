# 转岗返聘业务服务

现有能力是人员有效岗位查询、证书工时盘点、培训名册核对和月度工资核算。岗位期间为左闭右开，同一人同一天只属于一个岗位；离岗后可重新入职。培训名册只记录已安排课程，不改变人员岗位或授予证书。`POST /api/appointment` 保存岗位记录；`GET /api/capacity?date=2026-11-01&skill=bathing` 查询有效证书对应的合同容量；`GET /api/cost?date=2026-11-01` 核对人员成本。

## 人才供给分析

在在册人数之外新增可直接用于编制讨论的供给口径：未来各业务月、各院区、各资质可提供的护理工时（分钟），而不是离职人数预测。

* 人员履历按人串成时间线，调院、休假、转管理岗、恢复一线、离岗返聘是同一条履历上的事件，不把每段岗位当成独立新员工。
* 观察截止日（`history_quality.observation_until`）之后未发生的事件按右删失处理：没有结束记录不代表永不离开。只有人事登记日期（`effective_date_known=false`）的记录与生效日明确的记录分开：前者仍计入确定性供给并逐格打 `registration_only` 标记，但不参与精确留存时间估计。
* 留存用 Kaplan–Meier 曲线（Greenwood 区间）估计，返聘参与度用 Wilson 区间；院区/资质分层去重人数不足 5 人或事件不足时不给看似精确的数，回退整体曲线并显式标注 `insufficient_evidence`、`pooled_curve_fallback`。新院区与少见资质组合因此只会得到带说明的保守结论。
* 流向台账（`flow_ledger`）逐条标注 `dated`（有日期证据）或 `registration_only`（只能作假设）；返聘目标等方案参数统一打 `rehire_target_assumption` 等假设标记。

接口与命令：

* `GET /api/supply?anchor=2026-11-01&horizon_months=6` 返回流向台账、留存证据、确定性基线供给、风险调整期望供给（含区间或证据缺口标记）、培养产出与需求缺口（未提供需求口径时缺口留空，不臆造）。
* `POST /api/scenario-compare` 在相同人员起点、同一观察截止日和业务月份下并排比较方案。方案参数：`mentor_extra_minutes`（按课程延长带教，在课程覆盖月扣减带教者当期一线工时，扣减不低于零）、`extra_learners`（新增进修名额，学员只在课程结束当月取得资质，开课当月不计供给）、`rehire_rate_override`（返聘参与度目标，0–1，显式标为假设）。证书到期、合同工时与薪酬口径在所有方案中保持同一约束，同一人同一月不会出现在两个院区。
* `POST /api/snapshot` 保存编制会方案快照：写入时计算基础记录指纹，快照写后不可覆盖；补录经历后 `GET /api/snapshot?id=...` 会显式标 `stale`，但原样返回保存当时的数字与假设解释，重新分析须另存新快照。`GET /api/snapshots` 列出全部快照。
* 命令行：`python3 -m career supply --anchor 2026-11-01 --horizon 6`、`python3 -m career scenario --input 方案.json`、`python3 -m career snapshots`。

## 启动与核对

使用 Python 3.11 或更高版本。现有服务采用标准库运行；数值分析所需的固定版本工具列在 requirements.txt，部署准备时运行 `python3 -m pip install -r requirements-build.txt -r requirements.txt`，构建前端和打包后端的固定版本见 requirements-build.txt；依赖准备完毕后，测试、构建和运行均可断网进行。无需真实个人信息、凭据或远程数据源。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q career tests
python3 -m build --wheel --no-isolation
python3 -m career check
python3 -m career demo
python3 -m career load --database /tmp/career-business.sqlite3 --input examples/operations.json
python3 -m career serve --database /tmp/career-business.sqlite3
```

服务默认监听本机 8765 端口，`--port` 可指定其他端口。健康检查为 `GET /health`，`GET /api/summary` 返回当前已有业务汇总。未指定数据库时，demo 和 check 使用隔离的临时数据库；serve 若需保留修改须传入数据库路径。已有数据库不重复装载同一类样例。examples/operations.json 是较大的一组历史运营资料，可用 load 命令在事务内载入；按类型和编号更正同名记录，未出现的记录保持不变。所有资料采用固定来源生成且不包含对未来分析结论的预设值。

## 数据与运行边界

fixtures 中均为本地合成业务资料，员工、家庭和院区编号不对应真实对象。JSON 里的空值表示未记录，不代表零；日期采用 ISO 格式。金额以分保存，工时以分钟保存。每类记录有独立编号，写入同编号用于业务更正，revision 用于并发修改核对。

业务状态由 SQLite 保存，记录批量更正在单一事务内提交。接口使用 UTF-8 JSON，未知资源返回 404，非法数据返回 400，版本冲突返回 409。服务不进行临床诊断，也不自动改变实际服务安排。测试使用临时数据库和系统分配的空闲端口，彼此隔离，不访问公网。运行日志、数据库和依赖缓存不应提交到仓库。

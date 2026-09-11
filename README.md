# 国际物流全流程跟踪系统（ILTS）

面向**国际货运代理 / 外贸操作员**的工程项目全流程跟踪桌面工具：**项目 → 批次 → 15 节点**排期、单证齐套、位移联动、多批次资源冲突提示、日报周报生成。

- 技术栈：Python 3.12（本机全局）+ PySide6（Qt6）+ SQLite（标准库 `sqlite3`），纯本地离线（不接实时 AIS / 船司船期 / 在线地图）
- 运行：`py -3.12 app.py`（首次自动建库并灌入演示数据）
- 演示数据：1 个进行中项目（3 个批次）+ 1 个已完成历史项目

---

## 1. 核心概念

| 概念 | 说明 |
| :-- | :-- |
| **项目 Project** | 一次工程物流委托（如「青岛→巴西 Sepetiba 光伏组件运输」），状态 `Active / Completed / Cancelled` |
| **批次 Batch** | 项目下的一票货（一个航次 / 一批柜），状态 `draft / ready / running / 建议完成 / closed / cancelled`。**多批次是常态**：同一项目分批发运，船期常相差几天 |
| **节点 Node** | 每批次固定 15 个标准节点，用**稳定标识 `node_key`** 定位业务规则（不再比较序号）：提空箱 → 装箱/加固 → 返场/集港 → 出口报关 → 装船 → 海上运输 → 到货通知 → 换单 D/O → 进口许可 → 进口清关 → 海关查验 → 堆存费 → 内陆运输 → 工地交付 → 还空箱 |
| **单证 Doc** | 分两类，见 §3：**批次级**（票货单证，每批次各一份）与**项目级**（项目共用一份） |
| **今日** | 全局统一时钟 `services.clock`；顶栏「测试时间」可切模拟日期，所有入库时间戳的日期跟随模拟日期 |

---

## 2. 界面说明

### 2.1 启动页（精简版）
一行统计 + 「项目 · 批次」列表：

```
国际物流全流程跟踪系统                       [新建项目] [生成报告] [已完成]
项目 → 批次 → 15 节点全流程跟踪 · 单证齐套 · 资源冲突提示

[进行中项目 1] [启用中批次 3] [今日待办 54] [逾期节点 2] [缺单证 120]

项目 · 批次                                     已完成 1 个项目（进「已完成」页）
┌ 青岛→巴西Sepetiba 光伏组件运输  ID · demo-qd-br-001 · 3 批次  [主看板] [全批次总览] ┐
│ ● P-DEMOQD600-B01 已就绪 09-15→10-26  票货 0/39 项目级 0/3 逾期 2 ⚠冲突 3   甘特 › │
│ ● P-DEMOQD600-B02 已就绪 09-20→10-31  票货 0/39 项目级 0/3                 甘特 › │
│ ● P-DEMOQD600-B03 已就绪 09-25→11-05  票货 0/39 项目级 0/3                 甘特 › │
└────────────────────────────────────────────────────────────────────────────┘
```

- 统计口径：**启用中批次** = `draft/ready/running`；**今日待办** = 各启用中批次待办合计（点击看明细）；**缺单证** = 批次级未交必填累加 + 项目级只算一次。
- **点批次行** → 直接打开该批次的甘特工作台；**点「全批次总览」** → 直接打开总览视图；点「主看板」→ 进主看板。
- 已取消项目留在项目列表并标注（不进「已完成」页），页脚给出已完成 / 已取消入口说明。

### 2.2 主看板
**恒定高度摘要卡（约 180px、不可折叠）**，只回答「这个项目现在什么状况」：

```
▎批次 [● P-DEMOQD600-B01 ▾] [已就绪]  3批次·逾期2·待办42   [甘特工作台][新增批次][复制批次]
  🇧🇷 青岛→巴西Sepetiba 光伏组件运输   当前·节点3 返场/集港     ETD 09-15 · 距6天  [打开工作台]
  ID · demo-qd-br-001               ▓境内0/4▓▓海运0/1▓▓境外0/10▓  进度 0/15 · 缺单证 42
  出口港·青岛港  🚢 COSCO INTEGRITY                          货物 2 项 · 位移留痕：…
```

- 卡片不再承载甘特与操作面板（历史教训见 §8）；顶部另有一行项目级统计（进行中 / 今日截止 / 逾期 / 缺单证，按**全部启用中批次**聚合）与「多批次甘特」入口。
- **新增批次** = 建空白批次；在「批次管理」确认 ETD/ETA 保存后**自动按 15 节点模板生成节点与单证清单**。

### 2.3 甘特工作台（非模态独立窗口）

| | 单批次（可操作） | 全批次（只读总览） |
| :-- | :-- | :-- |
| 横轴 | 三区相对轴（境内逐日 · **海运压缩** · 境外逐日） | **真实日历轴**（日 / 周缩放） |
| 行 | 15 个节点（角色泳道、瓶颈/风险/待上游标签、今日金框） | **每个启用中批次一行** |
| 行内 | 节点色块 + 状态标签 | **三段色块**（境内/海运/境外真实跨度）+ 条首提交状态色帽 |
| 右列 | — | 批次号 · 状态 · 票货 x/39 · 项目级 y/3 · 逾期 / ⚠冲突 |
| 操作 | 勾单证 / 推迟提前 / 撤销 / 补齐节点 | **无**（隐藏右栏；点某行跳到该批次的单批次视图） |
| 冲突 | 底部「本批次资源风险」 | 红色虚线竖带 + 涉及行淡红底 + 底部跨批次冲突条 |
| 顶栏 | 进行中 / 逾期 / 缺单证 / 货物 字牌 | `N 批次 · 全提交 x · 部分提交 y · 未提交 z · 逾期节点 w` |

- 非模态：可与看板并存；卡片切批次 / 新增批次会同步刷新窗口；批次切换走顶部「批次」下拉（工作台内**没有**左侧批次卡片）。
- 状态记忆：模式 / 刻度 / 右栏收纳 / 标签 / 窗口几何写入 `settings`；关闭即隐藏（实例保留）。
- 「导出图片」把当前甘特存 PNG（默认 `data/reports/`）。
- 空批次不显示空白：甘特画占位卡（212px，永不 0 高度）+ 时间轴栏「补齐计划节点」一键生成。

### 2.4 已完成页 / 生成报告页
- **已完成页**：只读甘特查看历史批次（关键节点、单证齐套、完成日期、操作留痕）。
- **生成报告页**：选项目 / 批次 / 客户 → 「预览报告」只看不改 → 「生成」导出 docx（未装 `python-docx` 则导出同名 .txt）。板块：总体概览、未完成清单（批次/客户/柜号列）、操作动态、单证最新状态、下一个工作日待办（周报为下周待办）、智能摘要、（周报）与上周对比、下周风险。

### 2.5 顶栏
- **今日待办徽标**：`今日待办 N（M 批次）`——点击看明细，右键进提醒中心。
- **提醒中心**：按批次分组（免箱期倒计时 / 到货通知 / 申报截止 / 依赖等待）。
- **测试时间**：切模拟「今日」，便于演示逾期与预警。

---

## 3. 单证：批次级 vs 项目级

| | 批次级（票货单证） | 项目级（项目共用） |
| :-- | :-- | :-- |
| 存储 | `files`（`batch_id` 非空） | `project_files`（同项目一份，`(project_id, doc_name)` 唯一索引） |
| 判据 | 有节点锚点（`due_node_key` / `due_type` / `due_rule`） | **无任何锚点** |
| 巴西模板 | 46 份（必填 39）：订舱委托、SO、VGM、出口报关单、提单、装箱单… | 3 份（必填）：项目日报、物流动态跟踪表、项目进度报告 |
| 提交 | 每批次各自提交 | **提交一次全批次共享**（日志 `batch_id` 为空、`scope='project'`） |
| 提醒 | 每批次各一条并标注批次 | 只一条（不随批次重复） |
| 报告 | 按批次列行 | 只出现一次，批次列显示「项目级」 |
| 缺证统计 | 按批次累加 | **只算一次** |

> 《批次实施计划》《人员安排计划》虽写在 `config.py` 的 `files_project`，但挂了 `EXPORT_CUSTOMS` 锚点、到期日按各批次自己的报关节点算 → 仍属**批次级**。

---

## 4. 规则与算法

### 4.1 计划日期（`services/schedule2.py`，唯一来源）
- 以 **ETD / ETA** 为锚：海运段 `start = ETD`、`end = ETA`，时长 = ETA − ETD。
- **境内倒排**（自 ETD 逆推）、**境外顺排**（自 ETA 推进）；`WORKDAY` 节点跳过周六日，`NATURAL` 连续。
- 船期变更（`schedule_change`）按 A/B/C/D 分类，给出影响预览并留痕 `batch_schedule_changes`；`Done` 或已填实际完成日的节点**冻结**，不参与重算。

### 4.2 位移（推迟 / 提前）
- 境内 1–4 联动 ETD 与海运；海运节点 5 联动 ETA 与境外全段；境外 6–12 口岸整段平移。
- **四守卫**：提前不早于今日、已完成节点保护、链连续性（锚点不漂移）、位移历史可撤销（净位移可负）。
- 位移后锚在节点起止日的单证 `due_date` 自动重算。

### 4.3 自动完成
某节点**必填单证全部提交且已过 `plan_end`** → 自动 `Done`（逾期/红条消失）；撤勾必填单证自动回退。

### 4.4 资源冲突（`services/resource_conflict.py`，只读）

| 规则 | 级别 | 依据 |
| :-- | :-- | :-- |
| `PORT_WINDOW` 同出口港境内作业窗口重叠 | high | `batch_routes.export_port` + 境内节点区间 |
| `CUSTOMS_BROKER` 同报关行报关窗口重叠 | high | `customs_broker` + `EXPORT_CUSTOMS` 区间 |
| `VESSEL_VOYAGE` 同船名航次撞期 | high | 批次级 `vessel(vessel_name, voyage)` + `SEA_TRANSIT` 区间 |
| `FREE_TIME` 免堆/免箱截止日早于后续节点计划 | high/medium | `free_demurrage_until` / `free_detention_until` vs 该批次节点（**批次内自检**） |
| `DEST_STORAGE` 境外堆存/查验窗口重叠 | low | `CUSTOMS_INSPECT` / `STORAGE_FEE`（按目的国归组） |

### 4.5 提醒（`services/reminder.py`）
- 三级：`P0 需处理` / `P1 进行中提醒` / `P2 今日启动`；类型：节点逾期、节点今日到期/启动、单证超建议日、单证缺失、免堆/免箱倒计时、到货通知、保险到期、申报截止（AMS/ISF/ENS）、依赖等待（待上游）。
- **徽标口径** = **各启用中批次待办合计**（`draft/ready/running`，不含已取消/已完成）。
- **多批次区分**：每条提醒都带 `batch_id / batch_no / batch_name / batch_status`；条目 id 为 `rmd-<批次号>-<序号>`（跨批次唯一）；提醒中心按批次分组；今日待办行的标题是「项目 · 批次号」。

### 4.6 报告口径
- 「单证最新状态」按 **(批次, 单证名)** 取最后一次提交/撤销动作；项目级单证只出现一次（批次列「项目级」）。
- 「操作动态」逐条带批次；日报到小时、周报按天分组。
- 待办窗口：日报取次日（含周末提前预警），周报取下周一~周日。
- 编号 `RPT-YYYYMMDD-NNN` 按自然日独立重置。

---

## 5. 多批次下的统一口径（最容易踩的地方）

| 口径 | 规则 | 落点 |
| :-- | :-- | :-- |
| 缺单证 | 批次级未交必填**累加** + 项目级未交必填**只算一次** | `file_checklist.pending_required()` → 启动页 / 看板卡片 / 工作台字牌 / 看板顶部统计 |
| 提交进度 | `票货 x/39`（批次级）+ `项目级 y/3`（项目一份） | 启动页批次行、全批次总览行 |
| 提醒徽标 | 各启用中批次合计，UI 同时给出批次数 | `reminder.badge_count()` / 顶栏 / 启动页 |
| 操作日志收敛 | 单证：`(项目, **批次**, 单证, 日)` 一行最终态；位移：`(项目, **批次**, 节点, 日)` 净收敛；项目级单证 `batch_id` 为空 | `services/oplog.py` |
| 报告单证动作 | 按 `(批次, 单证名)` 取；项目级单独一行 | `db.last_file_actions_by_batch()` |
| 顶部统计 | 按项目内**全部启用中批次**聚合（与徽标同口径） | `DashboardPage._collect_stats()` |

---

## 6. 测试与验收

```powershell
# —— 专项校验（均使用隔离临时库，不碰 data/logistics.db）——
py -3.12 -X utf8 _home_check.py               # 启动页：统计口径 + 项目·批次列表 + 批次行点击
QT_QPA_PLATFORM=offscreen py -3.12 -X utf8 _verify_dash_layout.py   # 摘要卡/空批次补齐/新增批次端到端/全批次总览/导出
QT_QPA_PLATFORM=offscreen py -3.12 -X utf8 _opt_test_dash2col.py    # 工作台：两模式/联动/收纳/操作/双向同步
py -3.12 -X utf8 _verify_project_files.py     # 项目级单证：建表/CRUD/迁移幂等/不变量
py -3.12 -X utf8 _verify_reminder_batch.py    # 提醒与报告的批次区分（id 唯一 / op_log 不覆盖 / 报告不串批次）
py -3.12 -X utf8 _verify_conflict.py          # 资源冲突扫描五条规则 + 只读 + 容错

# —— 既有验收 ——
py -3.12 -X utf8 _opt_test_logic.py           # S1–S7 逻辑验收（位移/守卫/due/撤销/船位等）
QT_QPA_PLATFORM=offscreen py -3.12 -X utf8 _opt_test_ui.py   # UI 冒烟 + 甘特高亮
py -3.12 -X utf8 _opt_fix_check.py            # 右栏状态稳定 / 海运表头单调 / 单证自动完成
py -3.12 -X utf8 _opt_test_report.py          # 日报周报聚合 / 编号按日重置 / 单证口径
py -3.12 -X utf8 _clock_check.py _sim_e2e.py _reset_check.py _mig_check.py _verify_v2.py _verify_newpage.py _verify_filepanel.py

# —— 全量回归 / 总验收 ——
py -3.12 -X utf8 _audit/run_tests.py             # 默认回归集 18/18
py -3.12 -X utf8 _audit/run_tests.py --all       # 含可选脚本（28 个）
py -3.12 -X utf8 tools/test_acceptance_all.py    # T1–T40 总验收（40/40）
```

### 数据安全约定（血泪教训，务必遵守）
1. **测试脚本必须用隔离临时库**，且必须在 `import services` / 建连接**之前**设置：
   ```python
   _TMP = tempfile.mkdtemp(prefix="xxx_")
   db.DB_PATH = os.path.join(_TMP, "t.db"); db._conn = None; db.init_db()
   ```
   历史上曾有两个脚本直接操作真实库并把它清空：
   - `_verify_v2.py`：`os.remove("data/logistics.db")` 后重建；
   - `_verify_newpage.py`：清理语句写成 `WHERE project_name LIKE '__%'`，而 SQLite 里 `_` 是**单字符通配符** → 等于匹配所有项目 → 删光全部项目与批次。
   两者均已改为临时库（后者同时把 LIKE 改成按名称精确匹配）。
2. `_audit/run_tests.py` 与 `tools/test_acceptance_all.py` 启动时会把真实库备份为 `data/logistics.db.auto-<时间戳>`（保留最近 3 份）；还原只需复制回 `data/logistics.db`。
3. 按设计仍直接读真实库的脚本（只读或仅清 `op_log`）：`_home_check.py`、`_verify_filepanel.py`、`_smoke3.py`、`_smoke_batch_ui.py`、`_report_preview.py`（只读）、`_reset_check.py`（清 `op_log`，演示日志下次启动自动重灌）。

---

## 7. 演示与调试

```powershell
py -3.12 -X utf8 _seed_multi_batch_demo.py   # 幂等：复制样板批次并整体推迟 N 天，用于演示全批次总览与资源冲突
py -3.12 -X utf8 _shot_dash.py               # 截图：摘要卡 / 工作台单批次 / 全批次总览 / 空批次
py -3.12 -X utf8 tools/reset_ops.py          # 只清 op_log（其余数据不动）
py -3.12 -X utf8 _report_preview.py          # 用真实库打印报告全文，目检排版
```

---

## 8. 目录结构

```
app.py                      入口：初始化 DB + 演示数据 + 启动 GUI
config.py                   国家模板（目的国单证/节点差异）+ 出口港薄壳
db.py                       SQLite 数据层（建表/迁移/CRUD/op_log/project_files）
mock_data.py                演示数据（进行中项目，可播种多批次）
mock_completed.py           演示数据（已完成历史项目）
services/
  node_template.py          15 节点模板（node_key 稳定标识，唯一业务锚点）
  schedule2.py              §5.3 计划日期唯一算法（境内倒排 / 海运=ETA−ETD / 境外顺排）
  schedule_change.py        船期变更 A/B/C/D 分类 + 影响预览 + 留痕
  scheduler.py              位移四守卫 + 撤销 + 位移历史
  node_status.py            节点状态机 + 「必填齐且过期末」自动完成
  file_checklist.py         单证清单 bootstrap（批次级 / 项目级拆分）+ 缺证口径
  docdict.py / doc_dependency.py   单证类型主数据 / §10.5 依赖（上游未完成 → 下游待上游）
  reminder.py               §10.2/10.3/10.4 提醒（P0/P1/P2 + 批次上下文 + 徽标口径）
  reporting.py              ★ 报告聚合纯逻辑（概览/未完成/动态/单证最新状态/待办/摘要/周对比）
  report_exporter.py        报告导出 docx（可选）/ txt + 编号 RPT-YYYYMMDD-NNN
  resource_conflict.py      ★ 跨批次资源冲突扫描（五条规则，只读）
  oplog.py                  ★ 操作日志统一入口（白名单 + 当日收敛，收敛键含批次）
  ports.py / ports_cn.py    港口检索与资料（40 港）
  clock.py / cargo_check.py / vessel_status.py / validation.py / modes.py / timezone_helper.py / customs_stats.py
tools/
  migrate_batches.py        旧库（无批次）→ 批次化迁移：--check / --apply / --rollback
  reset_ops.py              测试辅助：只清 op_log
  gen_ports.py              可选：从 md 一次性导入生成 ports_cn.py
ui/
  main_window.py            侧栏 + 顶栏（今日待办徽标 / 提醒中心 / 测试时间）+ 页面栈
  gantt_workbench.py        ★ 甘特工作台（非模态窗口：单批次可操作 / 全批次只读总览）
  dialogs.py                货物台账 / 班轮船位 / 今日待办 对话框
  pages/                    home(启动页) / dashboard(主看板) / new_project / completed / report
  widgets/
    gantt_grid.py           单批次甘特（三区相对轴：境内逐日 · 海运压缩 · 境外逐日）
    gantt_overview.py       全批次总览（真实日历轴：行=批次 + 三段色块 + 提交状态）
    file_panel.py           单证清单（「项目级 · 全程常备」+ 批次节点分组，原地增量刷新）
    node_popover.py / port_map.py / mini_bar.py / scoped_scroll.py / dependency_view.py / reminder_center.py
    theme.py / icons.py / collapsible.py
```

---

## 9. 开发约定与 Qt 陷阱（踩过的坑）

1. **布局**：不要在卡片/头部用 `setFixedHeight()` 硬编码内容高度——QSS 的 `padding`（如 `QComboBox{padding:9px 12px}`）会让控件最小高大于设计值，布局压缩后会吃掉 padding，文字被裁。用 `setMinimumHeight()` + 内容驱动；必要时给容器加 `QSizePolicy.Maximum`，防止它吸收外层多余空间。
2. **重建区块**：清空布局要 `takeAt` 后先判断 `item.widget()`——布局里常带 `QSpacerItem`，直接 `widget().deleteLater()` 会崩；销毁前先 `setParent(None)`，否则 `deleteLater()` 生效前旧控件仍会与新控件抢位。
3. **滚动区内刷新**：优先**原地更新**（`update_files()` / `update_data()`），不要 `QScrollArea.setWidget()` 换容器——那会同步析构正在发信号的控件（曾导致勾选单证崩溃卡死）。
4. **信号类型**：`file_id` 可能是整数（批次级）或 `'pf-<n>'`（项目级），信号声明用 `Signal(object, bool)`。
5. **自绘控件高度**：空数据时必须给**非零高度**（占位态），否则 `auto_height()==0` → 上层 `setFixedHeight(0)` → 整块消失。
6. **同日收敛/去重**：任何当日收敛逻辑都要把批次放进键里，否则多批次下会互相覆盖（单证提交、位移留痕都踩过）。

---

## 10. 变更历史

| 版本 | 主要改动 |
| :-- | :-- |
| **v6.10** | 工作台收敛为「单批次（可操作）/ 全批次（只读总览）」两模式（总览改为行=批次 + 三段色块 + 提交状态），并删掉工作台内左侧批次卡片；新增 `project_files` 把项目级单证改为**单库一份**（含一次性迁移）；修复提醒/报告的批次穿透（`op_log` 收敛键加批次、`last_file_actions_by_batch`、提醒条目 id 跨批次唯一、今日待办行显示批次）；启动页精简为「一行统计 + 项目·批次列表」；修掉 `_verify_newpage.py` 清空真实库的隐患 |
| v6.9 | 甘特与操作面板迁出卡片 → 非模态**甘特工作台**（卡片变恒定高度摘要卡）；新增资源冲突扫描（五条规则）与冲突标注；新增多批次合并视图（v6.10 被全批次总览取代）；`ensure_batch_nodes` 修「新增批次后甘特打不开」 |
| v6.8 | 卡片头部改内容驱动（修批次号被裁）；批次列表行高/表高自适应；空批次甘特占位（不再 0 高度）；`ensure_batch_nodes` 按模板补齐节点与单证 |
| v6.7 | 统一时钟（模拟日期下所有入库时间戳跟随模拟日期）；报告板块调整；新增 `tools/reset_ops.py` |
| v6.6 | 修「单证清单勾选卡死崩溃」（FilePanel 重写为原地增量刷新）；`op_log` 单证同日收敛为一行最终态 |
| v7.0 | 批次化升级（多式联运一期）：项目/批次两级模型、15 节点 `node_key` 口径、船期变更 A/B/C/D、批次取消与恢复、报告批次维度 —— 新增国家改 `config.py` 即可 |

---

> 仓库仅包含代码与 README；业务/设计文档（多式联运、海港/清关说明、PDF 管理办法等）与运行时数据库仅保存在本地，不入库（见 `.gitignore`）。

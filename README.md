# Pillar Two 全球最低税测算工具（本地确定性引擎 + 可选云端 Agent）

一个**本地运行**的 GloBE（支柱二）测算工具：导入辖区数据 → 校验 → 计算 ETR / 补税 →
QDMTT·IIR·UTPR 三层分配 → 结果审查 → 情景模拟（含架构调整与组合搜索）→ 报告与 GIR 导出。

**数字只有一个来源**：`calculator.py`。云端 Agent（可选）只负责"提动作、写解读、规划图表"，
**不产生任何数值**。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | **3.12**（依赖按 3.12 固定；3.10+ 一般也能跑） |
| 操作系统 | Windows / macOS / Linux 均可 |
| 浏览器 | Chrome / Edge / Firefox 任一现代浏览器 |
| 网络 | **非必需**（不配 API Key 也能完整使用本地功能，见第 4 节） |

## 2. 获取代码

二选一：

```bash
# 方式一：git 克隆（推荐，方便以后更新）
git clone https://github.com/GAN-jun-jpg/pillar-two-tool.git
cd pillar-two-tool

# 方式二：不用 git —— 仓库页面点绿色 [Code] → Download ZIP，
#         解压后进入该目录（.env.example 是隐藏文件，看不到也不影响运行）
```

**后面所有命令都要在这个目录里执行。**

## 3. 安装（通用步骤）

```bash
# ① 建虚拟环境（推荐，避免污染系统 Python）
python -m venv .venv

# ② 激活虚拟环境
#    Windows (PowerShell)
.venv\Scripts\Activate.ps1
#    Windows (CMD)
.venv\Scripts\activate.bat
#    macOS / Linux
source .venv/bin/activate

# ③ 升级 pip（重要：Python 3.12 自带的 pip 过旧，装不了本项目锁定的版本）
python -m pip install --upgrade pip

# ④ 装依赖
pip install -r requirements.txt
```

> **国内网络建议加镜像**（本项目会拉 pandas / streamlit / pyarrow 等约 100 MB 的轮子，
> 直连 PyPI 可能只有几十 kB/s）：
> ```bash
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```
> 已装过一半中断了也没关系，重跑同一条命令会**续传**，不必删 venv 重来。

### 三个常见卡点（实测遇到过的）

| 现象 | 原因与解决 |
|---|---|
| `python : 无法将"python"项识别为...` | `python` 不在 PATH。改用 `py -3.12 -m venv .venv`，或写全路径 `<Python安装目录>\python.exe` |
| `.venv\Scripts\Activate.ps1 : 在此系统上禁止运行脚本` | 执行一次 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`；或**不激活**，直接用 `.venv\Scripts\python.exe` 代替 `python` |
| 装依赖报元数据/解析错误，或装完 `import pandas` 失败 | 第 ③ 步没做。先 `python -m pip install --upgrade pip` 再装 |

> **不想激活虚拟环境也可以**：把命令里的 `python` 换成 `.venv\Scripts\python.exe`（Windows）
> 或 `.venv/bin/python`（macOS/Linux）即可，效果一样。

## 4. 启动

```bash
streamlit run app.py
```

终端会打印访问地址（默认 <http://localhost:8501>），浏览器打开即可。
换端口：`streamlit run app.py --server.port 8599`。

**⚠️ 改了任何被 import 的模块（如 `calculator.py`）后必须重启** —— Streamlit 不会热重载模块，
只刷新页面会继续用旧代码。

## 4. 配置 API Key（可选）

**不配也能用**（见本节末尾对照表）。要启用云端 Agent，二选一：

### 方式 A（最简单，适合先试）：启动前设环境变量

在**同一个终端窗口**里先设变量、再启动（关掉窗口即失效）：

```powershell
# Windows (PowerShell)
$env:QWEN_API_KEY = "sk-你的key"
streamlit run app.py
```
```bash
# macOS / Linux
export QWEN_API_KEY="sk-你的key"
streamlit run app.py
```

### 方式 B（持久，推荐长期使用）：写进 `.env` 文件

```powershell
# Windows (PowerShell) —— 注意 .env.example 是隐藏文件，用命令行复制最省事
Copy-Item .env.example .env
notepad .env          # 填好你选的那组变量后保存
```
```cmd
:: Windows (CMD)
copy .env.example .env
```
```bash
# macOS / Linux
cp .env.example .env && nano .env
```

`.env` 与 `app.py` 放在**同一目录**（仓库根目录）即可，程序启动时会自动读取。

### Key 从哪里来

| 方式 | 去哪儿申请 |
|---|---|
| ① 通义千问 / DashScope（推荐，国内可直连） | 阿里云百炼控制台 → API-KEY 管理 → 创建 |
| ② 任意 OpenAI 兼容服务 | 自建网关 / 第三方服务商后台 |
| ③ DeepSeek | platform.deepseek.com → API Keys → 创建 |

`.env` 里**三选一**即可（推荐第一种）。**填了哪一组 key，就必须把 `LLM_DEFAULT_PROVIDER`
改成对应的名字**（只认这三个值）：

| 方式 | 需要的变量 | `LLM_DEFAULT_PROVIDER` 填 |
|---|---|---|
| ① 通义千问 / DashScope | `QWEN_API_KEY`（可选 `QWEN_MODEL`、`QWEN_BASE_URL`） | `dashscope` |
| ② 任意 OpenAI 兼容服务 | `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL_ID` | `openai_compatible` |
| ③ DeepSeek（默认） | `DEEPSEEK_API_KEY`（可选 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`） | `deepseek` |

> **不填 `LLM_DEFAULT_PROVIDER` 时默认用 `deepseek`** —— 也就是只填了千问的 key 却没改这一行，
> 会提示"没有 deepseek 的密钥"（不会崩，只是回退本地流程）。
**`.env` 已被 `.gitignore` 排除，永远不要提交。**

### 怎么确认配好了

启动后打开「运行」页签：云端相关按钮/开关可用即成功；
**若没配好，程序不会报错**，只是跳过云端解读与云端实验（本地计算、图表、报告全部照常）。

### 配置与不配置的区别

| 功能 | 不配 Key | 配了 Key |
|---|---|---|
| 数据导入 / 校验 / 计算 / 三层分配 | ✅ 完整可用 | ✅ |
| 结果页签、图表（ETR 柱 / 瀑布 / Sankey / 补税桥 / 风险矩阵） | ✅ 本地生成 | ✅ |
| 情景模拟、组合搜索、差异归因 | ✅ 完整可用 | ✅ |
| 报告 P1–P9、GIR 工作簿导出 | ✅ | ✅ |
| 审计留痕、规则版本管理 | ✅ | ✅ |
| 全部 **723 项测试** | ✅ `python -m pytest -q` | ✅ |
| Agent 工作流（Supervisor 编排 + 12 个 Agent） | ⚠️ 降级：本地确定性流程照常，云端解读/规划跳过 | ✅ 完整 |
| 云端设计实验（L2）、云端搜索范围建议 | ⚠️ 不可用（界面会提示，不会报错） | ✅ |

> 一句话：**没有 Key，工具照样算得对、出得了图和报告**；Key 只是多加"云端解读与实验规划"。

## 5. 加载示例数据

仓库自带两份示例数据：

| 文件 | 用途 |
|---|---|
| `25辖区测试数据_方案B_QDMTT落地.xlsx` | 25 个辖区，QDMTT 落地场景 |
| `DTL税务递延明细表.xlsx` | 递延税明细（用于验证递延税封顶与回转） |

两种加载方式：

1. **直接看现成方案**：启动后打开"数据"页签 → 方案库 → 选择 **方案A / 方案B**
   （来自 `data/pillar_two.db`）。
2. **自己走一遍导入**：数据页签 → 上传上面任一 Excel → 自动识别表头单位 →
   核对列映射 → 保存为方案 → 切到"运行"页签点计算。

> 注：少数测试还会用到 `25辖区测试数据_方案A_118条台账.xlsx`，该文件**不随仓库提供**；
> 缺失时相关测试会自动跳过（其余 700+ 项照常运行）。

**用在真实业务前**：把演示库与真实数据分开，用环境变量指向另一个库文件

```bash
# Windows (PowerShell):  $env:PILLAR_TWO_DB = "D:\path\to\real.db"
export PILLAR_TWO_DB=/path/to/real.db
```

并按财年核对口径（SBIE 过渡率、安全港门槛都是逐年变化的）。

## 6. 运行测试

```bash
python -m pytest -q
```

预期：**723 passed**（约 1–2 分钟）。若本地还有其它目录也放了测试，可跳过：
`python -m pytest -q --ignore='某个目录'`。

改代码时的规矩（血泪教训，详见 `GOTCHAS.md`）：
**每修一个 bug，都补一条"能复现它"的测试，并做变异验证（把修复改回去，测试必须变红）。**

## 7. 目录结构（关键文件各自负责什么）

| 文件 | 职责 |
|---|---|
| `app.py` | Streamlit 界面（数据 / 运行 / 结果 / 情景 / 审计 五个页签） |
| `calculator.py` | **唯一的税务计算实现**：ETR、SBIE、补税、QDMTT→IIR→UTPR 分配、税源流向 |
| `compute_pipeline.py` | 计算管线入口（界面与情景引擎共用，避免两份实现漂移） |
| `validator.py` | 算前校验（W/E/I 三档，29 条规则，规则库 JSON 驱动） |
| `scenario_engine.py` | 情景引擎（纯函数）：patch、校验、运行、对比、归因、扫描、结构动作 |
| `search.py` | 组合搜索（坐标下降 / 束搜索 / 子空间穷举 / 预算与缓存 / 诚实声明） |
| `entities.py` | 实体层：同辖区多实体聚合 + 归属份额（多母公司 IIR 分摊） |
| `visualizer.py` | 图表（ETR 柱、瀑布、Sankey、计算桥、风险矩阵、归因桥） |
| `analysis_report.py` | 报告 P1–P9（Markdown / Word） |
| `gir_exporter.py` | GIR 工作簿导出 |
| `storage.py` / `Agent/rules/` | 方案库（SQLite）/ 规则库（JSON，参数与校验码的唯一来源） |
| `Agent/` | Agent 框架（Supervisor + 12 个 Agent + A2A 消息总线 + 审计） |
| `test_*.py` | 723 项测试 |

## 8. 哪些数字可以信

| 保障 | 说明 |
|---|---|
| 单一来源 | 所有数值只由 `calculator.py` 产出；界面、情景、搜索、报告、GIR 调用同一条管线 |
| 官方向量 | `test_oecd_vectors.py` 用 OECD 官方案例锁定关键机制（含 Art 2.3.2 抵免） |
| 结果审查门禁 | `Agent/tools/result_review_tool.py` 做确定性复核（分配守恒、净负债构成、税源三去处、**图表数字**）；审查不通过则不产出图表 |
| 对账巡检 | `test_reconciliation.py` 把"界面上必须成立的关系"逐条锁死 |
| 审计留痕 | 每次运行、规则发布、情景保存都写审计日志（"审计"页签） |

## 9. 边界（做不到的事）

- **实体层**：支持"同一辖区多实体"与**改设 / 新增 / 删除辖区**（结构动作），但没有长期维护
  一张实体表的编辑界面；GIR 与报告仍按**辖区**口径；
- **多母公司**：支持按持股拆分 IIR（含上层 Art 2.3.2 抵免），但同一辖区多路径的完整官方
  算例未做；
- **规则库**：覆盖主规则（ETR / SBIE / 三层分配 / 安全港 / 递延税封顶与回转），
  **未覆盖**部分可选条款与特殊情形；`mapping_rules.json` 的所得税费用拆分仍缺更多附注场景；
- **组合搜索**：单目标；改设目标按 ETR 取前 3 个；结论口径只会说"该子空间内最优 / 局部最优 /
  启发式"，**从不声称全局最优**；
- **"调低利润"不是优化**：搜索把它标为非税务手段且默认不选，并默认要求"集团利润 + 覆盖税额不变"；
- **云端不可用时**：只跑本地确定性流程，图表与计算照常（刻意保留的兜底）。

## 10. 想接着做的话

1. 先读 [`GOTCHAS.md`](GOTCHAS.md)（**已知坑**，比功能清单值钱）；
2. 改功能时**一次只动一处**，跑全量测试后再提交。

## 11. 免责声明

本工具是税务计算与情景分析工具，计算结果**仅供分析与演示**，不构成税务意见，
也不能替代专业判断。使用前请核对财年口径（SBIE 过渡率、安全港门槛逐年变化）、
数据来源与规则适用性；作者不对任何基于本工具做出的税务申报或商业决策承担责任。

## 12. 许可

[MIT](LICENSE)

# 推送前隐私扫描报告（privacy_check）

- 扫描日期：2026-09-06
- 扫描人：codex cli（本地执行端）
- 范围：全部 git 历史（7 个提交）+ 当前跟踪清单（17 个文件）+ 待入库新文件（README.md、docs/eval.md、docs/privacy_check.md、scripts/run_eval.py、scripts/seed_profiles.example.py、tests/fixtures/sample_diaries.txt、tests/ 等）
- 扫描方式：`git log/grep` 全历史 blob 扫描 + 待入库文件正则扫描 + 人工抽查 prompts/system.txt、static/index.html、llm.py、scripts/seed_profiles.example.py、tests/fixtures

## 检查项结论

| 检查项 | 方法 | 结论 |
|---|---|---|
| 历史 blob 中的 sk- 密钥 | `git grep -nE "sk-[a-zA-Z0-9]{20}"` 逐 commit | ✅ 0 命中 |
| 待入库文件 sk- 密钥 | grep 正则 | ✅ 0 命中 |
| 环境变量带真实 key | grep `(DEEPSEEK_API_KEY\|OPENAI_API_KEY\|API_KEY)=sk-` | ✅ 仅 README 中 `sk-xxx` 占位示例，无真实 key |
| 真实姓名 | grep | ✅ 0 命中 |
| 手机号 | grep `1[3-9][0-9]{9}` | ✅ 0 命中 |
| 真实邮箱 | grep 邮箱形态 | ✅ 0 命中（无 example.com 之外的真实邮箱） |
| 硬编码 key（人工抽查） | llm.py / static/index.html / prompts/system.txt | ✅ key 全部走环境变量，无硬编码 |
| .gitignore 隐私排除 | 读取 .gitignore | ⚠️ 已覆盖新文件，但对**已跟踪文件无效**（见下） |

## .gitignore 隐私排除清单（含理由）

- `venv/`：本地虚拟环境，不入库
- `data/`：真实 agent.db、向量库、eval 测试库产物
- `imports/`：真实日记 txt
- `scripts/seed_profiles.py`：真实冷启动档案（示例见 .example 版）
- `.env`：本地 API key
- `.browser-print/`：浏览器导出数据
- docs/ 4 个内部规划与求职材料（工程表 PDF/HTML、resume_snippets.md、V2.6 任务书）：不入库，公开仓库只留代码、README 与 eval 报告

## 历史提交核查结论（重点 ⚠️）

发现**已跟踪敏感路径**，需主人决策后才能公开：

1. `git ls-files` 显示当前索引仍跟踪：
   - `imports/2026-08-01.txt`、`imports/2026-08-15.txt`、`imports/2026-09-01.txt`（真实日记）
   - `scripts/seed_profiles.py`（真实冷启动档案，8.3KB）
   - `prompts/reflect.txt`（0 字节空文件，无内容风险）
2. 对应历史提交：
   - `5699362 V1: 日记导入+RAG检索+焊死版聊天`（引入 imports/ 真实日记）
   - `81dac77 V2.1: 冷启动档案灌库脚本（33条profiles）`（引入真实 seed_profiles.py）
3. `.gitignore` 只能阻止**新增**未跟踪文件，对已跟踪文件无效——这些文件即使加 ignore，也会随现有历史被推上公开仓库。

## 未发现的问题

- docs/ 4 个内部文件从未进入 git 历史（本次 .gitignore 追加后不会再入库）。
- 全历史无 sk- API key。
- 待入库新文件（README/run_eval/fixtures/docs 报告）密钥与 PII 均 0 命中。

## 处理建议（未执行，待主人决策）

- 方案 A：重写历史（如 git-filter-repo / 新建孤儿分支）剔除 imports/、seed_profiles.py 后再公开——需主人明确授权，禁止 codex 自行改写历史。
- 方案 B：放弃公开现有历史，公开前新建干净仓库（只含筛选后的文件快照）。
- 在决策前：**不 commit、不 push**，本地文件原样保留。

## 历史清理记录（2026-09-07，方案 B：孤儿分支重建）

- 问题：旧历史（V1/V2.1 共 7 个提交）曾跟踪 imports/ 下 3 个真实日记文件与 scripts/seed_profiles.py 真实档案；.gitignore 对已跟踪文件无效。
- 决策：仓库从未 push，主人决定放弃旧历史，用孤儿分支重建——新历史仅 1 个提交，物理上不含敏感文件。
- 执行：敏感文件已备份至仓库外 ~/personal-agent-backup-20260906/（imports/ 全量 + seed_profiles.py）；`git checkout --orphan` 重建索引，白名单 18 个文件精确入库；旧引用 reflog expire + gc --prune=now --aggressive 清除。
- 复验：`git rev-list --count HEAD` = 1；`git ls-files` 无敏感路径；全历史 blob 尸检（见下）PII/密钥 0 命中；工作区 imports/、data/、seed_profiles.py 原样保留，本地 agent 功能不受影响。

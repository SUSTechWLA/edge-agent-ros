# 参与 Edge Agent ROS 开发

仓库用 Git 留存每次实现，用 SDD（Specification-Driven Development，规格驱动开发）描述行为、契约和验收证据。以“导航失败时保留可查询原因”为例，改动的交付物是具体错误事件、兼容的读取接口、能复现失败的测试，以及关联它们的一份规格。

## 从问题到 PR

1. **复现并定位。** 记录版本、平台、输入、预期与实际结果。现场任务优先带上任务 ID、操作 ID、脱敏错误事件。检查已有 issue 和 [规格目录](docs/specs/)。
2. **先写验收，再改实现。** 从 [SDD 模板](docs/specs/TEMPLATE.md) 建立 `docs/specs/SDD-NNN-short-name.md`。沿用同一契约的修复也可更新已有规格。按[插件开发规范](docs/plugin-development.md)选择接入口，写出 Definition / Provider / Consumer、能复测的 `AC-1` 等条件、装卸边界、失败时机、旧任务和旧数据如何处理。每次功能变更都更新「变更记录」，写明改动原因、路径、验证和残余范围。
3. **在短期分支提交。** 从目标分支新建 `feat/sdd-NNN-slug`、`fix/sdd-NNN-slug` 或 `docs/slug`。提交名和 PR 标题用 Conventional Commit，如 `fix(gateway): preserve stop timeout evidence`。提交正文写 `Spec: SDD-NNN`，涉及多个规格则逐一列出。配置自己的仓库 Git 身份。保留既有提交作者及 issue/PR 关联。
4. **复测。** 本地先执行下方快速检查；涉及 ROS 或机器人行为时再执行容器验收。为每个 AC 在规格中关联命令、输入、观测结果及证据路径。仿真、指令集模拟和物理板测试分开记录。原始仓库检出应能够找到已签入的证据。大体积的新运行日志可留在 CI artifact 并引用 run。
5. **发起 PR。** 在 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 写明对应 SDD 编号、验收结果和兼容性。PR 审查核对每个功能改动是否由相应规格覆盖。CI 会检查实现差异、规格变更记录和 PR 引用；行为是否满足规格由测试和评审确认。

```sh
git switch main
git switch -c fix/sdd-003-navigation-timeout
# 更新关联规格，例如 docs/specs/SDD-003-simulation-system-tests.md
python3 scripts/check_sdd.py --base main
python3 tests/repository_contract.py -v
python3 tests/planning_contract.py -v
git diff --check
# git add 选择本次改动；提交后核查完整差异
git diff --stat main...HEAD
```

Windows PowerShell 可将 `python3` 换为可用的 Python 3 解释器。提交前运行 `python3 tests/system_suite.py --changed-since origin/main --plan`，将所改文件、选中的消费者和验收条件逐一对照。去掉 `--plan` 即可执行同一组测试。在新增跨模块依赖的同一条 PR 里补上选择规则和对应断言。CI 在 PR 上按影响范围测试，在主分支、发布分支和手动运行时做全套确定性回归。需要真实模型时，在本机显式传入模型缓存卷并保留报告。[选测实例与故障矩阵](docs/testing/single-edge.md) · [工作流](.github/workflows/ci.yml)

```powershell
# 使用独立 Compose 项目，在临时机器人上测试停车、重启与控制链路故障
./scripts/verify.ps1
docker run --rm --network none edge-agent-ros:v0.1 python3 -m unittest discover -v -s /opt/edge/console/server
python tests/console_resilience.py
```

## 插件变更怎样评审

[插件开发规范](docs/plugin-development.md)是接入和装卸的详细约定；[AGENTS.md](AGENTS.md)约束 Coding Agent，PR 使用相同标准：

- **改动范围合理。** 新业务放在提供者中，消费者依公开契约接入。复用既有入口；确需改宿主时，说明为何已有扩展点不足、影响哪些已上线模块。
- **可替换性有证据。** 检查新旧请求、依赖缺失、断连、换版、卸载和恢复。不要把“描述符可读”写成“服务健康”，或把进程重启写成透明热替换。
- **旧行为能继续。** 独立观察原操作调用次数、任务/归档身份和无关任务结果。版本绑定不包含提供者二进制，行为变化必须更换版本并记录实现身份。
- **选测覆盖消费者。** PR 列出消费者与测试夹具依赖，记录选测计划及实测结果。新增依赖同步更新选择规则和断言；公共执行契约、未知路径与发布候选遵守扩大回归要求。
- **可部署、可撤回、可追溯。** 提供具体命令、数据保留方式、回退后的检查与 AC 证据。新增术语遵循[术语映射](docs/harness-extensions.md#团队共用的术语)，目标能力与当前实现分开写。

## 只改文档时

纯 README、规范、模板或注释性说明变更可执行以下检查，并在变更记录说明没有运行代码差异：

```sh
python tests/document_contract.py -v
python tests/repository_contract.py -v
python scripts/check_sdd.py --base <开发基线>
git diff --check
```

仍查看选测计划。当前选择器对 `.github/PULL_REQUEST_TEMPLATE.md` 等未分类路径会保守地选全量；若仅为人工阅读模板，可记录差异审查后采用文档检查，不必为此改测试规则或操作日常机器人。**CI workflow、提示、工具清单、现场策略、部署配置不属于这一例外**。本地减测理由不能替代 PR 已要求的检查，也不能推断运行行为已重新验收。

## 分支、评审和回移

日常使用同一个 `edge-agent-ros` 目录。长期保留以下两条分支。需要独立评审时，在该目录用 `git switch -c` 创建临时分支，合入开发线后删除。部署生成器直接更新 Git 引用。查看板端版本及手册来源可运行 `git show deploy/robot:ROBOT_SOURCE`。本地运行日志进入 `.ci-results/`，可共享的证据和复测入口留在开发分支。

| 引用 | 职责 | 合入方式 |
|---|---|---|
| `main` | 源码、研发文档、仿真、测试、验收记录和板端手册 | 在此集成，自已验收的开发提交生成部署版本和发行标签 |
| `deploy/robot` | 板端运行文件和 `ROBOT_SOURCE` 来源指针 | 在开发分支运行 `python scripts/release_robot.py HEAD`，保留上一版部署提交作为父提交 |

2026-11-19 整理前的分支头与独立验收快照保存在 `archive/2026-11-19/` 标签下。使用 `git tag -l 'archive/2026-11-19/*'` 查找，用 `git show <标签>:<路径>` 读取当时的文件。

单维护者模式的默认规则要求 PR、CI 和讨论解决，独立批准数为 0。团队有第二位维护者时，可把规则中的 `required_approving_review_count` 提高为 1。合并后从 issue/PR → SDD → 容器证据可查测试，从 `git log --follow -- docs/specs/SDD-001-runtime-trace.md` 或 `git log --all --grep=SDD-001` 可查契约演变。

## 连接 GitHub 与启用保护

将项目推至组织或个人指定的仓库后，设置默认分支为 `main`，推送生成的 `deploy/robot`，并在仓库中启用 Actions。先让两个检查在正常 PR 或手工运行中出现，再启用保护。[GitHub rulesets API](https://docs.github.com/en/rest/repos/rules)

具有仓库管理权限的维护者可以在 **Settings → Rules → Rulesets** 导入下列两份 JSON，也可用已认证的 GitHub CLI 调用 API。先用 `gh repo view --json nameWithOwner` 核对目标仓库。以下命令在项目根目录执行一次。后续更新相应规则时对其现有 ID 用 `PUT` 以避免重复创建。

```sh
gh api 'repos/{owner}/{repo}/rulesets' --paginate --jq '.[] | [.id, .name, .enforcement] | @tsv'
gh api --method POST 'repos/{owner}/{repo}/rulesets' --input .github/rulesets/branches.json
gh api --method POST 'repos/{owner}/{repo}/rulesets' --input .github/rulesets/tags.json
gh api 'repos/{owner}/{repo}/rules/branches/main'
```

[分支保护配置](.github/rulesets/branches.json)固定两个来自 GitHub Actions 应用的检查，限制删除及强推。[标签保护配置](.github/rulesets/tags.json)限制修改和删除发布标签。公开发布前由项目所有者确定适用的许可证与数据分发范围。机器人现场消息、真实运行卷和密钥均应留在部署环境。

2026-11-17 核验时，`SUSTechWLA/edge-agent-ros` 为私有仓库，Actions 已启用，规则集 API 因当前套餐返回 HTTP 403。当前维护者通过 PR 查看并等待两个检查完成后合并。拥有受支持套餐时，可应用上面两份配置启用服务器强制约束。

## 发布一个版本

在开发分支对齐 `src/**/package.xml` 里的版本、发行说明和兼容性。切换到已推送并验收的开发提交，创建附注标签。以下命令以版本 0.1.1 为例。

```sh
git fetch origin --tags
git switch --detach origin/main
git tag -a v0.1.1 -m "Edge Agent ROS 0.1.1"
python3 scripts/check_release.py v0.1.1
git push origin refs/tags/v0.1.1
```

该标签触发同一套 CI。两个检查通过后，发布任务再次验证标签、包版本和开发线归属，创建 **Draft Release** 并关联 Git SHA 和本次 CI run。维护者核对变更清单、验收和发布范围后在 GitHub 发布草稿。回退部署时选择之前验证过的部署提交及相应数据备份，修复按新的补丁版本发布。[GitHub CLI 发布行为](https://cli.github.com/manual/gh_release_create)

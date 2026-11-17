# 参与 Edge Agent ROS 开发

仓库用 Git 留存每次实现，用 SDD（Specification-Driven Development，规格驱动开发）描述行为、契约和验收证据。以“导航失败时保留可查询原因”为例，改动的交付物是具体错误事件、兼容的读取接口、能复现失败的测试，以及关联它们的一份规格。

## 从问题到 PR

1. **复现并定位。** 记录版本、平台、输入、预期与实际结果。现场任务优先带上任务 ID、操作 ID、脱敏错误事件。检查已有 issue 和 [规格目录](docs/specs/)。
2. **先写验收，再改实现。** 从 [SDD 模板](docs/specs/TEMPLATE.md) 建立 `docs/specs/SDD-NNN-short-name.md`。沿用同一契约的修复也可更新已有规格。写出能复测的 `AC-1` 等条件、涉及的模块、失败时机、旧数据如何处理。每次功能变更都更新「变更记录」，写明改动原因、路径、验证和残余范围。
3. **在短期分支提交。** 从目标分支新建 `feat/sdd-NNN-slug`、`fix/sdd-NNN-slug` 或 `docs/slug`。提交名和 PR 标题用 Conventional Commit，如 `fix(gateway): preserve stop timeout evidence`。提交正文写 `Spec: SDD-NNN`，涉及多个规格则逐一列出。配置自己的仓库 Git 身份。保留既有提交作者及 issue/PR 关联。
4. **复测。** 本地先执行下方快速检查；涉及 ROS 或机器人行为时再执行容器验收。为每个 AC 在规格中关联命令、输入、观测结果及证据路径。仿真、指令集模拟和物理板测试分开记录。原始仓库检出应能够找到已签入的证据。大体积的新运行日志可留在 CI artifact 并引用 run。
5. **发起 PR。** 在 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 写明对应 SDD 编号、验收结果和兼容性。PR 审查核对每个功能改动是否由相应规格覆盖。CI 会检查实现差异、规格变更记录和 PR 引用；行为是否满足规格由测试和评审确认。

```sh
git switch main
git switch -c fix/sdd-003-navigation-timeout
# 编辑 docs/specs/SDD-003-navigation-timeout.md、实现和回归测试
python3 scripts/check_sdd.py --base main
python3 tests/repository_contract.py -v
python3 tests/planning_contract.py -v
git diff --check
# git add 选择本次改动；提交后核查完整差异
git diff --stat main...HEAD
```

Windows PowerShell 可将 `python3` 换为可用的 Python 3 解释器。在 CI 上，`SDD and contracts` 检查规格、PR 标题、契约及两个 Compose 描述文件。`ROS and Gazebo` 构建项目镜像，执行 C++、ROS、Gazebo 故障验收及工作台回归。PR 验证使用只读令牌与模拟环境，不需要用户模型密钥。[工作流](.github/workflows/ci.yml)

```powershell
# 使用独立 Compose 项目，在临时机器人上测试停车、重启与控制链路故障
./scripts/verify.ps1
docker run --rm --network none edge-agent-ros:v0.1 python3 -m unittest discover -v -s /opt/edge/console/server
python tests/console_resilience.py
```

## 分支、评审和回移

| 引用 | 职责 | 合入方式 |
|---|---|---|
| `main` | 持续集成、下一版本的来源 | 功能分支开 PR；两个必需检查通过、讨论已处理后合入 |
| `release/<主>.<次>` | 某一版本线的维护，如 `release/0.1` | 从已验收的提交切出；从维护线新建修复分支，使用 `git cherry-pick -x <已合入修复>` 留存来源，再向维护线开 PR |
| `feat/*`、`fix/*`、`docs/*` | 一组可独立审查的改动 | 合入后可删除短期分支，提交或 squash 消息保留规格 ID 和 PR 编号 |
| `vX.Y.Z` | 发布时点 | 维护线的已验收提交创建附注标签，标签固定到该提交 |

单维护者模式的默认规则要求 PR、CI 和讨论解决，独立批准数为 0。团队有第二位维护者时，可把规则中的 `required_approving_review_count` 提高为 1。合并后从 issue/PR → SDD → 容器证据可查测试，从 `git log --follow -- docs/specs/SDD-001-runtime-trace.md` 或 `git log --all --grep=SDD-001` 可查契约演变。

## 连接 GitHub 与启用保护

将项目推至组织或个人指定的仓库后，设置默认分支为 `main`，推送需要维护的 `release/*`，并在仓库中启用 Actions。先让两个检查在正常 PR 或手工运行中出现，再启用保护。[GitHub rulesets API](https://docs.github.com/en/rest/repos/rules)

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

在维护线的 PR 内对齐 `src/**/package.xml` 里的版本、发行说明和兼容性。切换到目标提交，确认其属于已推送的相应维护线，创建附注标签。例如版本 0.1.1：

```sh
git fetch origin --tags
git switch --detach origin/release/0.1
git tag -a v0.1.1 -m "Edge Agent ROS 0.1.1"
python3 scripts/check_release.py v0.1.1
git push origin refs/tags/v0.1.1
```

该标签触发同一套 CI。两个检查通过后，发布任务再次验证标签、包版本、维护线归属，创建 **Draft Release** 并关联 Git SHA 和本次 CI run。维护者核对变更清单、验收和发布范围后在 GitHub 发布草稿。回退部署时选择之前验证过的标签和相应数据备份，修复按新的补丁版本发布。[GitHub CLI 发布行为](https://cli.github.com/manual/gh_release_create)

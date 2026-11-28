# 仓库协作要求

以下要求适用于本仓库中的 Appnet 工作。范围、网络架构和上线验收以 [APPNET.md](APPNET.md) 为准。

## Go 与 Cobra

- 使用 Go 实现 Appnet，交付 Linux 可执行文件 `appnet`。使用 `github.com/spf13/cobra` 构建根命令和子命令，按官方教程组织入口和 `cmd/`。参考 [Getting Started with Cobra](https://cobra.dev/docs/tutorials/getting-started/)。
- Cobra 命令负责参数、帮助和退出状态，网络逻辑放在独立的 `internal/` 包。用 `RunE` 返回错误。用 `go.mod` 明确最低 Go 版本和 Cobra 依赖版本，并保存 `go.sum`。
- 首版命令为 `check`、`attach <service>` 和 `detach <service>`。`check` 只读，接网和清理可以安全重试。容器生命周期由 systemd 管理，SNAT 和 DNAT 由宿主机持久化配置管理。
- 把特权操作限定在 root 管理的配置所授权的服务和资源内。核对实际调用方 UID、容器身份及当前 netns，操作经过验证的 netns 句柄。命令行只接受已授权的服务标识，日志包含失败环节。

## 命令执行与测试

- 禁止在用户的个人电脑上执行 Appnet 相关命令。这包括 Go 和 Cobra 的初始化、依赖下载、格式化、构建、运行和测试，以及 `podman`、`ip`、`nft`、`systemctl` 等 Appnet 验证命令。Cobra 教程中的命令也由用户自行决定何时执行。
- 禁止自主连接服务器进行 Appnet 构建、部署、诊断或测试，包括 SSH、远程执行接口和远程容器连接。所有实际执行和测试由用户完成。
- 助手可以通过工作区文件工具编辑源码、测试和文档，并提供由用户手动执行的验证步骤。交付时说明适用环境、执行身份、预期结果及回滚步骤，并将待用户执行的验证标记为待验证。

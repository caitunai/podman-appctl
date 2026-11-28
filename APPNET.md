# Appnet 首版方案

Appnet 面向现有两台 Linux 服务器，网络规划预留第三台。应用继续以 `www` 用户运行 rootless Podman，宿主机负责配置 Linux bridge、veth、路由和防火墙。接入 Appnet 的容器通过内核转发业务流量，绕开 `pasta`。

## 首版交付

- 两台主机上的容器可以分别通过对方的宿主机内网 IP 和发布端口，主动连接对方的服务。容器也能访问局域网里的 Redis、MySQL 等服务。
- 任一宿主机重启或两台同时冷启动后，宿主机网络和 rootless 容器自动恢复，无需用户登录。
- 容器使用手工分配的固定 IPv4 地址，重启和换镜像时沿用。同机容器通过本机 bridge 通信。
- 宿主机使用固定的 nftables DNAT 发布内网端口。

## 技术选型与命令行

Appnet 使用 Go 实现，交付目标为 Linux 上的 `appnet` 可执行文件。命令行基于 `github.com/spf13/cobra`，按官方教程组织根命令和子命令。入口调用 Cobra 的 `Execute`，`cmd/` 负责参数校验、错误和退出状态，Linux 网络操作放在独立的 `internal/` 包。实现时在 `go.mod` 中明确最低 Go 版本和 Cobra 版本，用 `go.sum` 记录依赖校验信息。参考 [Getting Started with Cobra](https://cobra.dev/docs/tutorials/getting-started/)。

首版命令随开机检查和容器生命周期设计，名称和职责如下。

| 命令 | 职责 |
| --- | --- |
| `appnet check` | 供系统服务检查本机 bridge、网关、IPv4 转发及所需防火墙规则，只读检查并报告未就绪的项目。 |
| `appnet attach <service>` | 通过受限特权入口，为注册服务的当前容器接入 veth，配置固定 IP 和默认路由。 |
| `appnet detach <service>` | 通过同一权限入口，清理该服务拥有的运行时网络资源。重复执行应安全。 |

子命令通过 Cobra `RunE` 返回可定位的错误。网络准备失败时返回非零退出码，供 systemd 判定启动结果。网络配置沿用宿主机对 iproute2、nftables 和 systemd 的管理方式，应用的进程监督由 systemd 负责。

## 网络规划

| 主机 | 宿主机内网 IP | Linux bridge | 容器子网 | 容器网关 |
| --- | --- | --- | --- | --- |
| `caitun001` | `172.17.37.116` | `br-app` | `10.201.1.0/24` | `10.201.1.1` |
| `caitun002` | `172.17.37.117` | `br-app` | `10.201.2.0/24` | `10.201.2.1` |
| 第三台（预留） | 待分配 | `br-app` | `10.201.3.0/24` | `10.201.3.1` |

部署前核对实际宿主机地址和出口网卡，确认容器子网与现有局域网、VPC、VPN、Podman 网络和路由均无重叠。每台主机只建一座本地 bridge。容器通过 veth 接入 bridge，宿主机内网网卡继续承载三层转发。

### 跨主机访问与端口发布

每台宿主机用 nftables DNAT 将自己的内网 IP 和端口映射到本机容器的固定地址。调用方只需要知道对方的宿主机内网 IP 和端口，也可以用现有内网 DNS 指向该宿主机。下面是两边各发布一个服务的示例。

| 发布主机 | 调用方使用的地址 | DNAT 目标 |
| --- | --- | --- |
| `caitun001` | `172.17.37.116:8090/tcp` | `10.201.1.20:8090` |
| `caitun002` | `172.17.37.117:8081/tcp` | `10.201.2.20:8081` |

跨主机流量由发起方宿主机做出站 SNAT，再由接收方宿主机做 DNAT 并转发到容器。两台主机均启用 IPv4 转发，在现有防火墙中放行实际需要的 TCP 或 UDP 端口及回程流量。入站来源可限定为对端宿主机内网 IP。发布前核对宿主机已有监听和 NAT 规则，并确认应用监听容器网卡上的目标端口。映射随宿主机启动加载，容器重启或换镜像时沿用。

Podman 原生 `--publish`（Quadlet `PublishPort=`）通过 Podman 管理的网络发布端口。当前默认使用 `pasta` 时，加上该参数仍会沿用 `pasta` 出站。Appnet 试点容器用 `--network=none` 创建，再接上宿主机管理的 veth，由宿主机 nftables 负责发布端口。这一路径的容器启动配置省略 Podman 原生端口映射。

参考 [Podman 5.4.2 网络选项](https://docs.podman.io/en/v5.4.2/markdown/podman-run.1.html#network-mode-net) 和 [Podman 5.4.2 端口映射实现](https://github.com/containers/podman/blob/v5.4.2/pkg/specgen/generate/namespaces.go#L283-L344)。

### 容器访问其他局域网服务

每台主机对本机容器子网经指定网卡出站的流量配置一条 SNAT 规则。主机内网地址固定时可配置明确的 SNAT 地址，也可按现有防火墙方式使用 masquerade。其他局域网服务看到的是发起方宿主机的内网 IP，Redis、MySQL 的来源白名单据此配置。

MySQL、持久化 Redis 的地址固定到实际承载数据的实例。使用现有内网 DNS 或应用配置记录这些地址和端口。

参考 [IPv4 转发配置](https://docs.kernel.org/networking/ip-sysctl.html) 和 [nftables NAT 文档](https://wiki.nftables.org/wiki-nftables/index.php/Performing_Network_Address_Translation_(NAT))。

### 加入第三台主机

第三台配置自己的 bridge、出站 SNAT 和所需的内网端口映射。现有主机按需更新访问地址及允许来源。访问 Redis、MySQL 等外部服务时，按需把第三台宿主机内网 IP 加入白名单。扩容沿用宿主机地址通信，各主机只需维护本地容器子网及本机的规则。

## 开机恢复与容器生命周期

### 宿主机网络

复用服务器现有的网络和防火墙管理方式，由 root 持久化 bridge、网关、IPv4 转发和 nftables 规则。启用一个 systemd 系统服务（本文称 `appnet-host.service`）作为开机就绪检查。它在负责配置网络和防火墙的系统服务之后运行 `appnet check`，等待本机内网网卡和地址、bridge 和网关、转发状态以及所需 SNAT、DNAT 和过滤规则就绪。用 `Type=oneshot` 和 `RemainAfterExit=yes` 记录检查成功。检查只依赖本机配置，另一台主机或数据库暂时离线时，本机网络仍可就绪。

网络规则由现有防火墙统一加载，保留现有规则和 SSH 等宿主机服务。验证防火墙正常重载后的实际转发，避免出现就绪检查已通过、所需规则随后被重载移除的情况。

### Rootless 容器启动

对试点服务启用 `www` 的 systemd linger，由独立的 systemd 用户服务管理容器启动、停止和重试。启动流程如下。

1. 显式等待系统服务 `appnet-host.service` 就绪，设置超时。`After=` 的排序作用域限于同一个 systemd 管理器，用户服务需要通过系统服务状态检查等待宿主机网络。
2. 以 `www` 身份使用 Podman，按需创建 `--network=none` 的容器，再用 `podman init` 完成初始化，取得本次启动所用的 netns。
3. 通过管理员设置的受限特权入口调用 `appnet attach <service>`，将 veth 的宿主机一端接入 `br-app`，在容器 netns 中启用 `lo` 和 veth，配置固定 IPv4 地址和默认路由。按本次启动的 netns 检查或建立连接。
4. 接网成功后再启动应用，由 systemd 监督容器生命周期。等待或接网失败时，本次启动失败并写入 journal。为用户服务设置重试间隔和启动速率限制，使开机阶段的暂时失败可以恢复。

内核网络资源由宿主机启动配置及容器启动流程重建。容器停止后通过 `appnet detach <service>` 清理归属明确的运行时资源，保留固定 IP 的配置。容器重启或替换时对新 netns 重新接网。重复启动或接网中断后重试，应能处理本服务残留的 veth，避免重复接口或误清理仍在使用的资源。应用连接远端依赖的重试由应用或其 systemd 服务管理。

验收时从容器内使用实际业务域名查询和建连。若默认解析配置不可用，可给试点容器只读挂载一份指向可达内网 DNS 的 `resolv.conf`。`--network=none` 与 Podman 的 `--dns` 参数互斥。

参考 [systemd linger](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)、[Quadlet 的启动依赖](https://docs.podman.io/en/v5.4.2/markdown/podman-systemd.unit.5.html#implicit-network-dependencies)、[Podman 的手工接网示例](https://containers.github.io/podman.io_old/old/community/meeting/notes/2021-10-05/Podman-Rootless-Networking.pdf#page=7) 和 [Podman DNS 参数](https://docs.podman.io/en/v5.4.2/markdown/podman-run.1.html#dns-ipaddr)。

### 配置与权限

- 宿主机持久化配置由 root 管理，内容包括 bridge、子网、网关、内网网卡、固定的 SNAT 与 DNAT、防火墙规则。
- 每个接入的服务只需一条由 root 管理的记录，包含服务标识、授权部署 UID、容器名和固定 IPv4 地址。端口发布采用固定宿主机防火墙配置。新增服务时检查地址和端口是否冲突。
- helper 只接受已授权的服务标识。它核对调用方 UID、容器身份和当前 netns，使用已验证并打开的 netns 句柄执行接网，限制操作范围为配置中的 bridge 和 veth。日志记录失败环节和服务标识。
- 同一部署 UID 下的容器视为同一信任域。试点容器按业务需要削减权限，尤其是 `CAP_NET_ADMIN` 和 `CAP_NET_RAW`。宿主机服务和跨主机流量通过现有防火墙限制来源和端口。

## 与 appctl 的衔接

首版先用独立用户服务完成试点容器的启动、换镜像和回滚。现有 `appctl` 继续负责其他服务。每个试点容器只由一个启动入口管理，切换前停止原有 Quadlet 单元，并保留镜像配置与数据挂载以便回滚。

当前 `appctl` 拒绝 drop-in 中的 `ExecStartPre=`，而 Quadlet 生成的 `podman run --replace --rm` 会替换事先初始化的同名容器。试点通过后，再根据验证过的 `create → init → attach → start` 生命周期设计最小的集成点。持久 netns 的 `Network=ns:…` 路径涉及目标 netns 所属 user namespace 的权限检查，留待有明确复用需求时验证。

后续接入 `appctl` 时，可以沿用 Compose `ports` 的写法声明发布需求，再与 root 管理的固定映射对照。端口规则仍由宿主机加载。首版直接配置这些规则，试点服务的日常启动和镜像更新只需操作容器。

参考 [现有 drop-in 校验](appctl.py)、[Quadlet 5.4.2 生成逻辑](https://github.com/containers/podman/blob/v5.4.2/pkg/systemd/quadlet/quadlet.go#L571-L586) 和 [Linux setns(2)](https://man7.org/linux/man-pages/man2/setns.2.html)。

## 上线验收

构建、运行、部署和测试由用户在其选定的环境中手动执行，网络与 systemd 验收在目标 Linux 主机上进行。开发助手可以编辑源码、测试和验收步骤。禁止开发助手在用户个人电脑上运行 Appnet 相关命令，也禁止自主连接服务器进行构建、部署或测试。交付时给出用户可执行的步骤和预期结果，记录尚待验证的项目。执行边界见 [AGENTS.md](AGENTS.md)。

| 场景 | 通过条件 |
| --- | --- |
| 同机及局域网访问 | 同一 bridge 上的容器互通。容器能用实际地址或业务域名发起 Redis、MySQL 等连接。 |
| 跨主机双向通信 | 两边容器分别通过对端宿主机的内网 IP 和发布端口主动建立新连接，覆盖实际使用的协议。 |
| 无人登录时冷启动 | 分别重启任意一台，再同时重启两台。宿主机网络和端口映射先恢复，随后容器自动启动，原地址可继续用于双向通信及局域网访问。 |
| 宿主机网络启动延迟 | 容器等待本机网络就绪。超时或接网失败可从日志定位，网络恢复后容器可自动重试启动。 |
| 容器重启和更新 | 异常退出、换镜像和接网中断后重试，固定 IP 恢复，业务可以重新连接，bridge 上的接口没有重复。 |
| 防火墙重载和回滚 | 正常重载后实际转发仍可用。停止试点并撤销相关配置、恢复原有 Quadlet 后，业务可回到原 Podman 网络，数据挂载可继续使用。 |
| 性能对比 | 用同一主机、镜像、业务负载及吞吐量，与现有 `pasta` 路径对比宿主机总 CPU 和 P95 延迟，确认纳入试点的出站和跨主机流量确实绕开 `pasta`。 |

先验证一台主机上的一个高流量服务，确认 `www`、实际 Podman/crun 版本以及现用的 user namespace 设置能跑通接网和冷启动。随后在第二台接入一个服务，完成双向和两台同时冷启动的验收。首版通过后再逐步迁移其他服务。

确实需要跨主机访问容器 IP 或保留容器源 IP 时，再评估静态路由和 SNAT 豁免。自动 IPAM、服务注册表、动态 DNS 视图、通用端口 CLI、常驻修复进程和多发行版适配留待出现实际需求时再评估。

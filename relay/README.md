# Cursor LLM 中转服务

让连不上 Cursor 的内网机器（运行 job-hunter）借一台能直连 Cursor 的服务器
（`<RELAY_HOST>`，例如一台公网云主机）访问 Cursor 模型。

```
job-hunter(内网) ──公司代理(<CORP_PROXY>)──▶ 中转服务(<RELAY_HOST>:443) ──直连──▶ Cursor 云端
```

## 一、在 `<RELAY_HOST>`（能直连 Cursor 的服务器）上部署

```bash
# 1. 把本目录(relay/)拷到中转服务器，例如 ~/cursor-relay
#    可用 scp / git / 或直接在服务器上新建这些文件
cd ~/cursor-relay

# 2. 配置密钥
cp .env.example .env
#   生成一个强 token：
openssl rand -hex 32
#   把它填进 .env 的 RELAY_TOKEN，并填好 CURSOR_API_KEY

# 3. 启动（绑定 443 需要 root）
sudo ./run.sh
```

首次会建虚拟环境、装依赖、生成自签证书，然后监听 `https://0.0.0.0:443`。

自检（在中转服务器本机）：
```bash
curl -sk https://127.0.0.1/health
# 期望：{"status":"ok","model":"composer-2.5","has_key":true}
```

> 想后台常驻：用 `tmux`/`screen`，或做成 systemd 服务（见文末）。

## 二、收紧云防火墙（重要）

中转服务暴露在公网 443。**把来源限制成“公司代理的出口 IP”**，不要对全网开放：

- 那个出口 IP 就是之前 `python3 -m http.server 443` 日志里打印的客户端 IP，
  也可以从本服务的访问来源看到。
- 云防火墙 → 443 规则 → 来源填 `公司代理出口IP/32`。

token + 来源 IP 双重限制后，别人即使扫到也进不来。

## 三、在内网机器（job-hunter）上启用中转

编辑 `job-hunter/.env`：

```ini
LLM_PROVIDER=cursor
# 指向中转服务（用中转服务器的公网 IP/域名）
CURSOR_RELAY_URL=https://<RELAY_HOST>
CURSOR_RELAY_TOKEN=<与中转服务器上 RELAY_TOKEN 完全一致>
CURSOR_RELAY_VERIFY_TLS=false       # 自签证书，不校验
# 到中转服务器要经公司代理出网
CURSOR_PROXY_URL=http://<CORP_PROXY_HOST>:<PORT>
```

重启 job-hunter 即可。设置了 `CURSOR_RELAY_URL` 后，所有 Cursor 调用都会经
公司代理转发到中转服务器执行，本机不再直接调 Cursor SDK，也不需要本机的 CURSOR_API_KEY。

## 四、（可选）systemd 常驻

在中转服务器上 `/etc/systemd/system/cursor-relay.service`：

```ini
[Unit]
Description=Cursor LLM Relay
After=network-online.target

[Service]
WorkingDirectory=/root/cursor-relay
ExecStart=/root/cursor-relay/run.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cursor-relay
sudo journalctl -u cursor-relay -f
```

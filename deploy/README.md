# 远端本地翻译模型部署（可选）

把翻译跑在内网一台机器上（示例：Apple Silicon Mac，16GB 内存即可跑 7B 量化模型），
本机通过 SSH 隧道访问。云端 API 仍作为兜底。

## 一、翻译服务器上

```sh
brew install ollama
mkdir -p ~/bin && cp ollama-guard.sh ~/bin/ && chmod +x ~/bin/ollama-guard.sh
# 启动一次服务并拉模型（guard 脚本也会在需要时拉起 serve）
ollama serve &          # 或由本机的保活 agent 拉起
ollama pull qwen3.5:9b-q4_K_M
```

## 二、本机（跑 ghostty-cc-bilingual 的机器）

```sh
# 1) 保活：远端 serve + SSH 隧道（改 plist 里的 your-server / YOUR_USER）
cp com.example.ollama-serve.plist  ~/Library/LaunchAgents/
cp com.example.ollama-tunnel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.ollama-serve.plist
launchctl load ~/Library/LaunchAgents/com.example.ollama-tunnel.plist

# 2) 验证
curl -s http://127.0.0.1:11435/v1/models

# 3) 跑验收测试（翻译质量 / 延迟 / 代码还原 / 指令防吞）
python3 test_local_translate.py --base http://127.0.0.1:11435/v1 --model qwen3.5:9b-q4_K_M --api ollama --no-think
```

## 三、接到 ghostty-cc-bilingual

```toml
chain = ["local", "deepseek"]

[local]
api = "ollama"
think = false
base_url = "http://127.0.0.1:11435/v1"
model = "qwen3.5:9b-q4_K_M"
api_key_env = ""
timeout = 180
```

## 运维

```sh
# 自愈验证：杀掉远端 serve 或本机隧道，launchd 会自动重拉
launchctl print "gui/$(id -u)/com.example.ollama-serve" | head -20
tail -f /tmp/ollama-serve.log /tmp/ollama-tunnel.log

# 停用
launchctl unload ~/Library/LaunchAgents/com.example.ollama-*.plist
```

要点：远端 ollama 只监听 127.0.0.1（不要开 `OLLAMA_HOST=0.0.0.0`），
跨机访问一律走 SSH 隧道，这样不向局域网暴露任何翻译接口。

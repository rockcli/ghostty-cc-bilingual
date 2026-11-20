#!/bin/sh
# ollama serve 保活包装：端口空闲才 exec serve。
# serve 进程一死，exec 的 shell 没了 → ssh 连接断开 → 本机 launchd 自动重连重启。
export PATH=/opt/homebrew/bin:$PATH
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0
while true; do
  if lsof -i :11434 >/dev/null 2>&1; then
    sleep 30
  else
    exec ollama serve
  fi
done

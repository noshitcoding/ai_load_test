#!/bin/sh
echo "=== Key2 Einzeltest ==="
curl -sk -D /tmp/k2h.txt \
  -H "Authorization: Bearer sk-D2sJIR0FPFyKbxJsrc6Q" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}' \
  https://litellm.hpepcai2.demo.local/v1/chat/completions
echo ""
echo "--- Headers ---"
cat /tmp/k2h.txt

#!/bin/sh
KEY="sk-rHbsli_66a0T8K6CpeHLbw"
URL="https://litellm.hpepcai2.demo.local/v1"
MODEL="Qwen/Qwen3-8B"

echo "=== 1) /chat/completions (normal) ==="
curl -sk -w '\nHTTP %{http_code} | %{time_total}s\n' \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}],\"max_tokens\":10,\"temperature\":0.2}" \
  "$URL/chat/completions"
echo ""

echo "=== 2) /chat/completions mit priority im body ==="
curl -sk -w '\nHTTP %{http_code} | %{time_total}s\n' \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}],\"max_tokens\":10,\"temperature\":0.2,\"priority\":0}" \
  "$URL/chat/completions"
echo ""

echo "=== 3) Rate-Limit Headers ==="
curl -sk -w '\nHTTP %{http_code} | %{time_total}s\n' \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}],\"max_tokens\":10}" \
  "$URL/chat/completions"
echo ""

echo "=== 4) /key/info ==="
curl -sk \
  -H "Authorization: Bearer $KEY" \
  "$URL/key/info"
echo ""

echo "=== 5) Schnelltest: 5 Requests hintereinander ==="
for i in 1 2 3 4 5; do
  START=$(date +%s%N)
  RESP=$(curl -sk -w '|%{http_code}|%{time_total}' \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Hi\"}],\"max_tokens\":5}" \
    "$URL/chat/completions")
  CODE=$(echo "$RESP" | grep -o '|[0-9]*|' | tr -d '|')
  TIME=$(echo "$RESP" | grep -o '[0-9.]*$')
  echo "  #$i: HTTP $CODE in ${TIME}s"
done

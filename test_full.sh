#!/bin/sh
KEY1="sk-rHbsli_66a0T8K6CpeHLbw"
KEY2="sk-Djo6NCDIzSfscZi3GDrc6Q"
URL="https://litellm.hpepcai2.demo.local/v1"
MODEL="Qwen/Qwen3-8B"
BODY='{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Hi"}],"max_tokens":5}'

echo "==========================================="
echo " LOAD TEST — Key1 vs Key2"
echo "==========================================="
echo "Key1: $KEY1"
echo "Key2: $KEY2"
echo "Start: $(date '+%H:%M:%S')"
echo ""

echo "=== Headers Key1 ==="
curl -sk -D- -o /dev/null \
  -H "Authorization: Bearer $KEY1" \
  -H "Content-Type: application/json" \
  -d "$BODY" "$URL/chat/completions" | grep -i 'x-litellm-key-rpm\|x-ratelimit-api_key\|x-litellm-key-tpm\|x-litellm-key-spend'
echo ""

echo "=== Headers Key2 ==="
curl -sk -D- -o /dev/null \
  -H "Authorization: Bearer $KEY2" \
  -H "Content-Type: application/json" \
  -d "$BODY" "$URL/chat/completions" | grep -i 'x-litellm-key-rpm\|x-ratelimit-api_key\|x-litellm-key-tpm\|x-litellm-key-spend'
echo ""

echo "=== Max-Parallel-Grenze Key1 ==="
for CONC in 20 30 40 50 60 80; do
  rm -f /tmp/mp_*.txt
  for j in $(seq 1 $CONC); do
    ( curl -sk -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $KEY1" \
        -H "Content-Type: application/json" \
        -d "$BODY" "$URL/chat/completions" > /tmp/mp_${j}.txt ) &
  done
  wait
  COK=0; C429=0; COTHER=0
  for j in $(seq 1 $CONC); do
    CODE=$(cat /tmp/mp_${j}.txt 2>/dev/null)
    if [ "$CODE" = "200" ]; then COK=$((COK+1))
    elif [ "$CODE" = "429" ]; then C429=$((C429+1))
    else COTHER=$((COTHER+1)); fi
  done
  echo "  K1 Parallel=$CONC -> OK=$COK 429=$C429 other=$COTHER"
done
echo ""

echo "=== Max-Parallel-Grenze Key2 ==="
for CONC in 20 30 40 50 60 80; do
  rm -f /tmp/mp_*.txt
  for j in $(seq 1 $CONC); do
    ( curl -sk -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $KEY2" \
        -H "Content-Type: application/json" \
        -d "$BODY" "$URL/chat/completions" > /tmp/mp_${j}.txt ) &
  done
  wait
  COK=0; C429=0; COTHER=0
  for j in $(seq 1 $CONC); do
    CODE=$(cat /tmp/mp_${j}.txt 2>/dev/null)
    if [ "$CODE" = "200" ]; then COK=$((COK+1))
    elif [ "$CODE" = "429" ]; then C429=$((C429+1))
    else COTHER=$((COTHER+1)); fi
  done
  echo "  K2 Parallel=$CONC -> OK=$COK 429=$C429 other=$COTHER"
done
echo ""

echo "=== 200 Requests sequentiell Key1 ==="
T1S=$(date +%s)
OK1=0; F1=0; R429_1=0; TSUM1=0; TMIN1=99999; TMAX1=0
for i in $(seq 1 200); do
  RESP=$(curl -sk -o /dev/null -w '%{http_code} %{time_total}' \
    -H "Authorization: Bearer $KEY1" \
    -H "Content-Type: application/json" \
    -d "$BODY" "$URL/chat/completions")
  CODE=$(echo "$RESP" | awk '{print $1}')
  MS=$(echo "$RESP" | awk '{printf "%d", $2*1000}')
  if [ "$CODE" = "200" ]; then
    OK1=$((OK1+1)); TSUM1=$((TSUM1+MS))
    [ "$MS" -lt "$TMIN1" ] && TMIN1=$MS
    [ "$MS" -gt "$TMAX1" ] && TMAX1=$MS
  else F1=$((F1+1)); [ "$CODE" = "429" ] && R429_1=$((R429_1+1)); fi
  [ $((i % 50)) -eq 0 ] && echo "  $i/200 OK=$OK1 Fail=$F1 429=$R429_1"
done
T1E=$(date +%s); T1D=$((T1E-T1S))
echo "  Key1: OK=$OK1/200 429=$R429_1 Dauer=${T1D}s avg=$((TSUM1/OK1))ms min=${TMIN1}ms max=${TMAX1}ms RPS=$(echo "scale=2;200/$T1D"|bc)"
echo ""

echo "=== 200 Requests sequentiell Key2 ==="
T2S=$(date +%s)
OK2=0; F2=0; R429_2=0; TSUM2=0; TMIN2=99999; TMAX2=0
for i in $(seq 1 200); do
  RESP=$(curl -sk -o /dev/null -w '%{http_code} %{time_total}' \
    -H "Authorization: Bearer $KEY2" \
    -H "Content-Type: application/json" \
    -d "$BODY" "$URL/chat/completions")
  CODE=$(echo "$RESP" | awk '{print $1}')
  MS=$(echo "$RESP" | awk '{printf "%d", $2*1000}')
  if [ "$CODE" = "200" ]; then
    OK2=$((OK2+1)); TSUM2=$((TSUM2+MS))
    [ "$MS" -lt "$TMIN2" ] && TMIN2=$MS
    [ "$MS" -gt "$TMAX2" ] && TMAX2=$MS
  else F2=$((F2+1)); [ "$CODE" = "429" ] && R429_2=$((R429_2+1)); fi
  [ $((i % 50)) -eq 0 ] && echo "  $i/200 OK=$OK2 Fail=$F2 429=$R429_2"
done
T2E=$(date +%s); T2D=$((T2E-T2S))
echo "  Key2: OK=$OK2/200 429=$R429_2 Dauer=${T2D}s avg=$((TSUM2/OK2))ms min=${TMIN2}ms max=${TMAX2}ms RPS=$(echo "scale=2;200/$T2D"|bc)"
echo ""

echo "=== 300 Requests parallel (30 concurrent) abwechselnd Key1/Key2 ==="
rm -f /tmp/kc_*.txt
T3S=$(date +%s)
for WAVE in $(seq 1 10); do
  for j in $(seq 1 15); do
    IDX=$(( (WAVE-1)*30 + j ))
    ( curl -sk -o /dev/null -w '%{http_code} %{time_total}' \
        -H "Authorization: Bearer $KEY1" \
        -H "Content-Type: application/json" \
        -d "$BODY" "$URL/chat/completions" > /tmp/kc_1_${IDX}.txt ) &
  done
  for j in $(seq 1 15); do
    IDX=$(( (WAVE-1)*30 + j ))
    ( curl -sk -o /dev/null -w '%{http_code} %{time_total}' \
        -H "Authorization: Bearer $KEY2" \
        -H "Content-Type: application/json" \
        -d "$BODY" "$URL/chat/completions" > /tmp/kc_2_${IDX}.txt ) &
  done
  wait
done
T3E=$(date +%s); T3D=$((T3E-T3S))
K1OK=0; K1F=0; K2OK=0; K2F=0
for f in /tmp/kc_1_*.txt; do
  CODE=$(awk '{print $1}' "$f" 2>/dev/null)
  [ "$CODE" = "200" ] && K1OK=$((K1OK+1)) || K1F=$((K1F+1))
done
for f in /tmp/kc_2_*.txt; do
  CODE=$(awk '{print $1}' "$f" 2>/dev/null)
  [ "$CODE" = "200" ] && K2OK=$((K2OK+1)) || K2F=$((K2F+1))
done
echo "  Key1: OK=$K1OK Fail=$K1F"
echo "  Key2: OK=$K2OK Fail=$K2F"
echo "  Dauer: ${T3D}s"
echo ""

echo "==========================================="
echo " FERTIG $(date '+%H:%M:%S')"
echo "==========================================="

#!/bin/sh
KEY1="sk-rHbsli_66a0T8K6CpeHLbw"
KEY2="sk-D2sJIR0FPFyKbxJsrc6Q"
URL="https://litellm.hpepcai2.demo.local/v1"
MODEL="Qwen/Qwen3-8B"
BODY="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Hi\"}],\"max_tokens\":5}"

echo "╔══════════════════════════════════════════════════════╗"
echo "║           LLM LOAD TEST — AUSFÜHRLICH               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "URL:    $URL"
echo "Model:  $MODEL"
echo "Key1:   ${KEY1}"
echo "Key2:   ${KEY2}"
echo "Start:  $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

#─────────────────────────────────────────────────
# TEST 1: Einzelne Requests — Baseline Latenz
#─────────────────────────────────────────────────
echo "═══ TEST 1: Baseline Latenz (10 sequentielle Requests, Key1) ═══"
SUM=0
for i in $(seq 1 10); do
  T=$(curl -sk -o /dev/null -w '%{time_total}' \
    -H "Authorization: Bearer $KEY1" \
    -H "Content-Type: application/json" \
    -d "$BODY" "$URL/chat/completions")
  MS=$(echo "$T * 1000" | bc | cut -d. -f1)
  SUM=$((SUM + MS))
  echo "  #$i: ${MS}ms"
done
echo "  Durchschnitt: $((SUM / 10))ms"
echo ""

#─────────────────────────────────────────────────
# TEST 2: Rate Limit Headers beider Keys
#─────────────────────────────────────────────────
echo "═══ TEST 2: Rate-Limit Headers beider Keys ═══"
echo "--- Key1 ---"
curl -sk -D /tmp/h1.txt -o /dev/null \
  -H "Authorization: Bearer $KEY1" \
  -H "Content-Type: application/json" \
  -d "$BODY" "$URL/chat/completions"
grep -i 'x-litellm-key-rpm-limit\|x-ratelimit-api_key\|x-litellm-key-tpm-limit\|x-litellm-key-spend\|x-litellm-model-group' /tmp/h1.txt
echo ""
echo "--- Key2 ---"
curl -sk -D /tmp/h2.txt -o /dev/null \
  -H "Authorization: Bearer $KEY2" \
  -H "Content-Type: application/json" \
  -d "$BODY" "$URL/chat/completions"
grep -i 'x-litellm-key-rpm-limit\|x-ratelimit-api_key\|x-litellm-key-tpm-limit\|x-litellm-key-spend\|x-litellm-model-group' /tmp/h2.txt
echo ""

#─────────────────────────────────────────────────
# TEST 3: 100 sequentielle Requests — Key1
#─────────────────────────────────────────────────
echo "═══ TEST 3: 100 sequentielle Requests (Key1) ═══"
OK1=0; FAIL1=0; TSUM1=0; TMIN1=99999; TMAX1=0
ERR_CODES1=""
T3_START=$(date +%s)
for i in $(seq 1 100); do
  RESP=$(curl -sk -o /tmp/r.txt -w '%{http_code} %{time_total}' \
    -H "Authorization: Bearer $KEY1" \
    -H "Content-Type: application/json" \
    -d "$BODY" "$URL/chat/completions")
  CODE=$(echo "$RESP" | awk '{print $1}')
  T=$(echo "$RESP" | awk '{print $2}')
  MS=$(echo "$T * 1000" | bc | cut -d. -f1)
  if [ "$CODE" = "200" ]; then
    OK1=$((OK1 + 1))
    TSUM1=$((TSUM1 + MS))
    [ "$MS" -lt "$TMIN1" ] && TMIN1=$MS
    [ "$MS" -gt "$TMAX1" ] && TMAX1=$MS
  else
    FAIL1=$((FAIL1 + 1))
    ERR_CODES1="$ERR_CODES1 $CODE"
  fi
  if [ $((i % 20)) -eq 0 ]; then
    echo "  $i/100 done (OK=$OK1, Fail=$FAIL1)"
  fi
done
T3_END=$(date +%s)
T3_DUR=$((T3_END - T3_START))
echo "  === Ergebnis ==="
echo "  OK:       $OK1 / 100"
echo "  Fehler:   $FAIL1  $ERR_CODES1"
echo "  Dauer:    ${T3_DUR}s"
if [ "$OK1" -gt 0 ]; then
  echo "  Latenz:   avg=$((TSUM1 / OK1))ms  min=${TMIN1}ms  max=${TMAX1}ms"
  echo "  Eff. RPS: $(echo "scale=2; $OK1 / $T3_DUR" | bc)"
fi
echo ""

#─────────────────────────────────────────────────
# TEST 4: 200 parallele Requests (50 parallel x 4 Wellen) — Key1
#─────────────────────────────────────────────────
echo "═══ TEST 4: 200 Requests parallel (50 concurrent x 4 Wellen, Key1) ═══"
rm -f /tmp/par_*.txt
T4_START=$(date +%s)
WAVE=0
TOTAL_OK=0; TOTAL_FAIL=0; TOTAL_429=0; TOTAL_500=0
for WAVE in 1 2 3 4; do
  echo "  Welle $WAVE: 50 Requests parallel..."
  for j in $(seq 1 50); do
    IDX=$(( (WAVE-1)*50 + j ))
    (
      RESP=$(curl -sk -o /dev/null -w '%{http_code} %{time_total}' \
        -H "Authorization: Bearer $KEY1" \
        -H "Content-Type: application/json" \
        -d "$BODY" "$URL/chat/completions")
      echo "$RESP" > /tmp/par_${IDX}.txt
    ) &
  done
  wait
  WOK=0; WFAIL=0; W429=0
  for j in $(seq 1 50); do
    IDX=$(( (WAVE-1)*50 + j ))
    if [ -f /tmp/par_${IDX}.txt ]; then
      CODE=$(awk '{print $1}' /tmp/par_${IDX}.txt)
      [ "$CODE" = "200" ] && WOK=$((WOK+1)) || WFAIL=$((WFAIL+1))
      [ "$CODE" = "429" ] && W429=$((W429+1))
    fi
  done
  TOTAL_OK=$((TOTAL_OK + WOK))
  TOTAL_FAIL=$((TOTAL_FAIL + WFAIL))
  TOTAL_429=$((TOTAL_429 + W429))
  echo "    OK=$WOK  Fail=$WFAIL  429=$W429"
done
T4_END=$(date +%s)
T4_DUR=$((T4_END - T4_START))
echo "  === Ergebnis ==="
echo "  OK:      $TOTAL_OK / 200"
echo "  Fehler:  $TOTAL_FAIL (davon 429: $TOTAL_429)"
echo "  Dauer:   ${T4_DUR}s"
echo ""

#─────────────────────────────────────────────────
# TEST 5: 200 parallele Requests — Key2 (Vergleich)
#─────────────────────────────────────────────────
echo "═══ TEST 5: 200 Requests parallel (50 concurrent x 4 Wellen, Key2) ═══"
rm -f /tmp/par_*.txt
T5_START=$(date +%s)
TOTAL_OK2=0; TOTAL_FAIL2=0; TOTAL_429_2=0
for WAVE in 1 2 3 4; do
  echo "  Welle $WAVE: 50 Requests parallel..."
  for j in $(seq 1 50); do
    IDX=$(( (WAVE-1)*50 + j ))
    (
      RESP=$(curl -sk -o /dev/null -w '%{http_code} %{time_total}' \
        -H "Authorization: Bearer $KEY2" \
        -H "Content-Type: application/json" \
        -d "$BODY" "$URL/chat/completions")
      echo "$RESP" > /tmp/par_${IDX}.txt
    ) &
  done
  wait
  WOK=0; WFAIL=0; W429=0
  for j in $(seq 1 50); do
    IDX=$(( (WAVE-1)*50 + j ))
    if [ -f /tmp/par_${IDX}.txt ]; then
      CODE=$(awk '{print $1}' /tmp/par_${IDX}.txt)
      [ "$CODE" = "200" ] && WOK=$((WOK+1)) || WFAIL=$((WFAIL+1))
      [ "$CODE" = "429" ] && W429=$((W429+1))
    fi
  done
  TOTAL_OK2=$((TOTAL_OK2 + WOK))
  TOTAL_FAIL2=$((TOTAL_FAIL2 + WFAIL))
  TOTAL_429_2=$((TOTAL_429_2 + W429))
  echo "    OK=$WOK  Fail=$WFAIL  429=$W429"
done
T5_END=$(date +%s)
T5_DUR=$((T5_END - T5_START))
echo "  === Ergebnis ==="
echo "  OK:      $TOTAL_OK2 / 200"
echo "  Fehler:  $TOTAL_FAIL2 (davon 429: $TOTAL_429_2)"
echo "  Dauer:   ${T5_DUR}s"
echo ""

#─────────────────────────────────────────────────
# TEST 6: Max Parallel herausfinden (Key1)
# Starte mit 10, dann 20, 30, 40, 50, 60, 80
#─────────────────────────────────────────────────
echo "═══ TEST 6: Max-Parallel-Grenze finden (Key1) ═══"
for CONC in 10 20 30 40 45 50 60 80; do
  rm -f /tmp/mp_*.txt
  for j in $(seq 1 $CONC); do
    (
      RESP=$(curl -sk -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $KEY1" \
        -H "Content-Type: application/json" \
        -d "$BODY" "$URL/chat/completions")
      echo "$RESP" > /tmp/mp_${j}.txt
    ) &
  done
  wait
  COK=0; CFAIL=0; C429=0
  for j in $(seq 1 $CONC); do
    if [ -f /tmp/mp_${j}.txt ]; then
      CODE=$(cat /tmp/mp_${j}.txt)
      [ "$CODE" = "200" ] && COK=$((COK+1)) || CFAIL=$((CFAIL+1))
      [ "$CODE" = "429" ] && C429=$((C429+1))
    fi
  done
  echo "  Parallel=$CONC → OK=$COK  Fail=$CFAIL  429=$C429"
done
echo ""

#─────────────────────────────────────────────────
# TEST 7: RPM-Limit testen — 100 Req so schnell wie möglich
#─────────────────────────────────────────────────
echo "═══ TEST 7: RPM Sprint — 100 Req sequentiell so schnell wie möglich (Key1) ═══"
T7_START=$(date +%s)
ROK=0; RFAIL=0; R429=0
for i in $(seq 1 100); do
  CODE=$(curl -sk -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $KEY1" \
    -H "Content-Type: application/json" \
    -d "$BODY" "$URL/chat/completions")
  [ "$CODE" = "200" ] && ROK=$((ROK+1)) || RFAIL=$((RFAIL+1))
  [ "$CODE" = "429" ] && R429=$((R429+1))
  if [ $((i % 25)) -eq 0 ]; then
    echo "  $i/100 (OK=$ROK Fail=$RFAIL 429=$R429)"
  fi
done
T7_END=$(date +%s)
T7_DUR=$((T7_END - T7_START))
echo "  === Ergebnis ==="
echo "  OK:      $ROK / 100"
echo "  429:     $R429"
echo "  Dauer:   ${T7_DUR}s"
echo "  Eff RPS: $(echo "scale=2; 100 / $T7_DUR" | bc)"
echo ""

#─────────────────────────────────────────────────
# TEST 8: Finale Headers nach Lastest (Verbleibende Limits)
#─────────────────────────────────────────────────
echo "═══ TEST 8: Aktuelle Rate-Limit Headers nach Lasttest ═══"
echo "--- Key1 ---"
curl -sk -D /tmp/hf1.txt -o /dev/null \
  -H "Authorization: Bearer $KEY1" \
  -H "Content-Type: application/json" \
  -d "$BODY" "$URL/chat/completions"
grep -i 'x-litellm-key-rpm\|x-ratelimit-api_key\|x-litellm-key-tpm\|x-litellm-key-spend' /tmp/hf1.txt
echo ""
echo "--- Key2 ---"
curl -sk -D /tmp/hf2.txt -o /dev/null \
  -H "Authorization: Bearer $KEY2" \
  -H "Content-Type: application/json" \
  -d "$BODY" "$URL/chat/completions"
grep -i 'x-litellm-key-rpm\|x-ratelimit-api_key\|x-litellm-key-tpm\|x-litellm-key-spend' /tmp/hf2.txt
echo ""

echo "╔══════════════════════════════════════════════════════╗"
echo "║                    FERTIG                            ║"
echo "╚══════════════════════════════════════════════════════╝"
echo "Ende: $(date '+%Y-%m-%d %H:%M:%S')"
